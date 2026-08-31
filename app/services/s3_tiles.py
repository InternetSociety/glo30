import asyncio
import math
import os
import re
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pyproj import Geod

from app.config import Settings
from app.exceptions import DemCoverageError, TileDownloadError
from app.models.cached_tile import CachedTile
from app.repositories.cached_tiles import CachedTileRepository

WGS84_GEOD = Geod(ellps="WGS84")
GLO30_PRODUCT_MARKER = "/COP-DEM_GLO-30-DGED/"
GLO30_PRODUCT_PATTERN = re.compile(
    r"^DEM1_SAR_DGE_30_(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})T\d{6}_"
)
GEOCELL_PATTERN = re.compile(
    r"^Copernicus_DSM_10_(?P<latitude_hemisphere>[NS])(?P<latitude>\d{2})_00_"
    r"(?P<longitude_hemisphere>[EW])(?P<longitude>\d{3})_00$"
)
RESTRICTED_GEOGRAPHY_MESSAGE = (
    "The geography you have requested is not yet released to the public. Please visit "
    "https://sentinels.copernicus.eu/-/copernicus-dem-30-metre-dataset-now-freely-available "
    "for more information"
)
UNAVAILABLE_GEOGRAPHY_MESSAGE = (
    "The geography you have requested is not available from Copernicus GLO-30"
)
MISSING_S3_OBJECT_ERROR_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


def copernicus_geocell(south: int, west: int) -> str:
    lat_prefix = "N" if south >= 0 else "S"
    lon_prefix = "E" if west >= 0 else "W"
    return f"Copernicus_DSM_10_{lat_prefix}{abs(south):02d}_00_{lon_prefix}{abs(west):03d}_00"


def object_key_for_geocell(tile_id: str, prefix: str) -> str:
    clean_prefix = prefix.removeprefix("/eodata/").strip("/")
    return f"{clean_prefix}/{tile_id}/DEM/{tile_id}_DEM.tif"


def geocell_center(tile_id: str) -> tuple[float, float]:
    match = GEOCELL_PATTERN.fullmatch(tile_id)
    if match is None:
        raise ValueError(f"Invalid Copernicus geocell: {tile_id}")

    south = int(match.group("latitude"))
    if match.group("latitude_hemisphere") == "S":
        south = -south
    west = int(match.group("longitude"))
    if match.group("longitude_hemisphere") == "W":
        west = -west
    return west + 0.5, south + 0.5


def ccm_prefix_for_product(product_name: str) -> str:
    match = GLO30_PRODUCT_PATTERN.match(product_name)
    if match is None:
        raise ValueError(f"Unsupported GLO-30 product name: {product_name}")
    return (
        "CCM/COP-DEM_GLO-30-DGED/SAR_DGE_30_A4AD/"
        f"{match.group('year')}/{match.group('month')}/{match.group('day')}/"
        f"{product_name}_"
    )


def glo30_product_names(payload: Any) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
        raise ValueError("Copernicus catalogue returned an invalid response")

    names: list[str] = []
    for product in payload["value"]:
        if not isinstance(product, dict):
            continue
        name = product.get("Name")
        s3_path = product.get("S3Path")
        if (
            isinstance(name, str)
            and isinstance(s3_path, str)
            and GLO30_PRODUCT_PATTERN.match(name)
            and GLO30_PRODUCT_MARKER in s3_path
        ):
            names.append(name)
    return names


def find_dem_object(
    s3_client: Any,
    bucket_name: str,
    product_names: list[str],
    tile_id: str,
) -> str | None:
    expected_suffix = f"/{tile_id}/DEM/{tile_id}_DEM.tif"
    paginator = s3_client.get_paginator("list_objects_v2")

    for product_name in product_names:
        for page in paginator.paginate(
            Bucket=bucket_name,
            Prefix=ccm_prefix_for_product(product_name),
        ):
            for item in page.get("Contents", []):
                object_key = item.get("Key")
                if isinstance(object_key, str) and object_key.endswith(expected_suffix):
                    return object_key
    return None


def is_missing_s3_object_error(error: ClientError) -> bool:
    error_code = str(error.response.get("Error", {}).get("Code", ""))
    status_code = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return error_code in MISSING_S3_OBJECT_ERROR_CODES or status_code == 404


def geocells_for_circle(
    longitude: float,
    latitude: float,
    radius_m: float,
    sample_interval_degrees: float,
) -> list[str]:
    """Return every one-degree geocell touched by a geodesic circle's bounds."""

    sample_count = math.ceil(360 / sample_interval_degrees)
    samples = [
        WGS84_GEOD.fwd(
            longitude,
            latitude,
            sample_index * sample_interval_degrees,
            radius_m,
        )[:2]
        for sample_index in range(sample_count)
    ]
    sampled_longitudes = [point[0] for point in samples]
    sampled_latitudes = [point[1] for point in samples]

    unwrapped_longitudes = [
        longitude + ((sampled - longitude + 180.0) % 360.0) - 180.0
        for sampled in sampled_longitudes
    ]

    south_start = max(-90, math.floor(min(sampled_latitudes)))
    south_stop = min(90, math.ceil(max(sampled_latitudes)))
    west_start = math.floor(min(unwrapped_longitudes))
    west_stop = math.ceil(max(unwrapped_longitudes))

    # A sub-cell circle still needs the cell containing its centre.
    if south_start == south_stop:
        south_stop += 1
    if west_start == west_stop:
        west_stop += 1

    geocells: set[str] = set()
    for south in range(south_start, south_stop):
        for unwrapped_west in range(west_start, west_stop):
            west = ((unwrapped_west + 180) % 360) - 180
            geocells.add(copernicus_geocell(south, west))
    return sorted(geocells)


class S3TileService:
    def __init__(
        self,
        repository: CachedTileRepository,
        settings: Settings,
        s3_client: Any | None = None,
        catalogue_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self._s3_client = s3_client
        self._catalogue_client = catalogue_client

    async def get_tiles(
        self,
        longitude: float,
        latitude: float,
        radius_m: float,
    ) -> list[Path]:
        tile_ids = geocells_for_circle(
            longitude,
            latitude,
            radius_m,
            self.settings.coverage_boundary_sample_interval_degrees,
        )
        restricted_tile_ids = sorted(
            tile_id for tile_id in tile_ids if tile_id in self.settings.glo30_restricted_tile_ids
        )
        if restricted_tile_ids:
            raise DemCoverageError(
                RESTRICTED_GEOGRAPHY_MESSAGE,
                log_detail=f"Restricted GLO-30 tile(s): {', '.join(restricted_tile_ids)}",
            )

        paths: list[Path] = []
        for tile_id in tile_ids:
            paths.append(await self._get_tile(tile_id))
        await self._remove_expired_tiles()
        return paths

    async def _get_tile(self, tile_id: str) -> Path:
        now = datetime.now(UTC)
        expires_at = now + timedelta(days=self.settings.tile_cache_expiry_days)
        destination = self.settings.tile_cache_dir / f"{tile_id}_DEM.tif"
        cached = await self.repository.get_by_tile_id(tile_id)

        if cached and self._as_utc(cached.expires_at) >= now:
            cached_path = Path(cached.file_path)
            if await asyncio.to_thread(cached_path.is_file):
                cached.last_used_at = now
                cached.expires_at = expires_at
                return cached_path

        object_key = cached.object_key if cached else await self._resolve_object_key(tile_id)
        await self._download(object_key, destination)

        if cached:
            cached.object_key = object_key
            cached.file_path = str(destination)
            cached.last_used_at = now
            cached.expires_at = expires_at
        else:
            await self.repository.add(
                CachedTile(
                    tile_id=tile_id,
                    object_key=object_key,
                    file_path=str(destination),
                    last_used_at=now,
                    expires_at=expires_at,
                )
            )
        return destination

    async def _resolve_object_key(self, tile_id: str) -> str:
        if self.settings.glo30_s3_prefix:
            return object_key_for_geocell(tile_id, self.settings.glo30_s3_prefix)

        longitude, latitude = geocell_center(tile_id)
        product_names = await self._catalogue_product_names(longitude, latitude)
        if not product_names:
            raise DemCoverageError(
                UNAVAILABLE_GEOGRAPHY_MESSAGE,
                log_detail=f"No GLO-30 catalogue product covers {tile_id}",
            )

        try:
            s3_client = await self._get_s3_client()
            object_key = await asyncio.to_thread(
                find_dem_object,
                s3_client,
                self.settings.s3_bucket_name,
                product_names,
                tile_id,
            )
        except (BotoCoreError, ClientError) as exc:
            raise TileDownloadError(f"Unable to search Copernicus S3 for {tile_id}") from exc
        if object_key is None:
            raise DemCoverageError(
                UNAVAILABLE_GEOGRAPHY_MESSAGE,
                log_detail=f"No GLO-30 DEM object was found for {tile_id}",
            )
        return object_key

    async def _catalogue_product_names(
        self,
        longitude: float,
        latitude: float,
    ) -> list[str]:
        query_filter = (
            "Collection/Name eq 'COP-DEM' and "
            "OData.CSC.Intersects(area=geography'SRID=4326;"
            f"POINT ({longitude} {latitude})')"
        )
        params = {
            "$filter": query_filter,
            "$select": "Name,S3Path",
            "$top": "100",
        }
        try:
            if self._catalogue_client is not None:
                response = await self._catalogue_client.get(
                    self.settings.copernicus_catalogue_url,
                    params=params,
                )
            else:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.get(
                        self.settings.copernicus_catalogue_url,
                        params=params,
                    )
            response.raise_for_status()
            return glo30_product_names(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise TileDownloadError("Unable to query the Copernicus catalogue") from exc

    async def _download(self, object_key: str, destination: Path) -> None:
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
        try:
            s3_client = await self._get_s3_client()
            await asyncio.to_thread(
                self._download_file,
                s3_client,
                self.settings.s3_bucket_name,
                object_key,
                temporary,
                destination,
            )
        except ClientError as exc:
            if is_missing_s3_object_error(exc):
                tile_id = destination.name.removesuffix("_DEM.tif")
                raise DemCoverageError(
                    UNAVAILABLE_GEOGRAPHY_MESSAGE,
                    log_detail=f"No GLO-30 DEM object was found for {tile_id}",
                ) from exc
            raise TileDownloadError(
                f"Unable to download GLO-30 tile for {Path(object_key).stem}"
            ) from exc
        except (BotoCoreError, OSError) as exc:
            raise TileDownloadError(
                f"Unable to download GLO-30 tile for {Path(object_key).stem}"
            ) from exc

    async def _remove_expired_tiles(self) -> None:
        now = datetime.now(UTC)
        for tile in await self.repository.list_expired(now):
            await asyncio.to_thread(Path(tile.file_path).unlink, missing_ok=True)
            await self.repository.delete(tile)

    @staticmethod
    def _download_file(
        s3_client: Any,
        bucket_name: str,
        object_key: str,
        temporary: Path,
        destination: Path,
    ) -> None:
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            s3_client.download_file(bucket_name, object_key, str(temporary))
            os.replace(temporary, destination)
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    async def _get_s3_client(self) -> Any:
        if self._s3_client is None:
            self._s3_client = await asyncio.to_thread(self._create_s3_client)
        return self._s3_client

    def _create_s3_client(self) -> Any:
        if self.settings.s3_access_key is None or self.settings.s3_secret_key is None:
            raise TileDownloadError("Copernicus S3 credentials are not configured")
        return boto3.client(
            "s3",
            endpoint_url=self.settings.s3_endpoint_url,
            aws_access_key_id=self.settings.s3_access_key.get_secret_value(),
            aws_secret_access_key=self.settings.s3_secret_key.get_secret_value(),
            region_name="default",
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 5, "mode": "standard"},
            ),
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
