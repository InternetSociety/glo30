import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import rasterio
from botocore.exceptions import ClientError, ReadTimeoutError

from app.config import Settings
from app.exceptions import (
    CopernicusConfigurationError,
    CopernicusTimeoutError,
    DemCoverageError,
    TileDownloadError,
)
from app.models.cached_tile import CachedTile
from app.services import s3_tiles
from app.services.s3_tiles import (
    COPERNICUS_CONFIGURATION_MESSAGE,
    COPERNICUS_TIMEOUT_MESSAGE,
    COPERNICUS_UNAVAILABLE_MESSAGE,
    GLO30_CATALOGUE_LAYOUT_UNSUPPORTED_CODE,
    GLO30_CATALOGUE_LAYOUT_UNSUPPORTED_MESSAGE,
    GLO30_TILE_UNAVAILABLE_CODE,
    GLO30_TILE_UNAVAILABLE_MESSAGE,
    ZERO_ELEVATION_OBJECT_KEY,
    S3TileService,
    catalogue_grid_id,
    copernicus_geocell,
    find_dem_object,
    geocell_center,
    geocells_for_circle,
    glo30_product_prefixes,
    object_key_for_geocell,
)

PRODUCT_NAME = "DEM1_SAR_DGE_30_20101226T173648_20140818T173725_ADS_000000_ri2b.DEM"
TILE_ID = "Copernicus_DSM_10_S40_00_E174_00"
RESTRICTED_TILE_ID = "Copernicus_DSM_10_N40_00_E044_00"
PRODUCT_PREFIX = (
    f"CCM/COP-DEM_GLO-30-DGED/SAR_DGE_30_A4AD/2010/12/26/{PRODUCT_NAME.removesuffix('.DEM')}"
)
OBJECT_KEY = f"{PRODUCT_PREFIX}/{TILE_ID}/DEM/{TILE_ID}_DEM.tif"


class InMemoryTileRepository:
    def __init__(self) -> None:
        self.tiles: dict[str, CachedTile] = {}

    async def get_by_tile_id(self, tile_id: str) -> CachedTile | None:
        return self.tiles.get(tile_id)

    async def list_expired(self, now: datetime) -> list[CachedTile]:
        return [tile for tile in self.tiles.values() if tile.expires_at < now]

    async def add(self, tile: CachedTile) -> CachedTile:
        self.tiles[tile.tile_id] = tile
        return tile

    async def delete(self, tile: CachedTile) -> None:
        self.tiles.pop(tile.tile_id, None)


class FakeS3Client:
    def __init__(self, listed_keys: list[str] | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.listed_keys = listed_keys or []
        self.list_calls: list[tuple[str, str]] = []

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        self.calls.append((bucket, key, filename))
        Path(filename).write_bytes(b"fake-geotiff")

    def get_paginator(self, operation: str) -> Any:
        assert operation == "list_objects_v2"
        client = self

        class FakePaginator:
            def paginate(self, **kwargs: str) -> list[dict[str, Any]]:
                client.list_calls.append((kwargs["Bucket"], kwargs["Prefix"]))
                return [{"Contents": [{"Key": key} for key in client.listed_keys]}]

        return FakePaginator()


class MissingS3Client(FakeS3Client):
    def download_file(self, bucket: str, key: str, filename: str) -> None:
        raise ClientError(
            {
                "Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "GetObject",
        )


def test_copernicus_geocell_uses_southwest_degree() -> None:
    assert copernicus_geocell(-40, 174) == "Copernicus_DSM_10_S40_00_E174_00"
    assert copernicus_geocell(0, -1) == "Copernicus_DSM_10_N00_00_W001_00"


def test_geocell_center_parses_hemispheres() -> None:
    assert geocell_center("Copernicus_DSM_10_S40_00_E174_00") == (174.5, -39.5)
    assert geocell_center("Copernicus_DSM_10_N00_00_W001_00") == (-0.5, 0.5)


def test_catalogue_grid_id_uses_tile_hemispheres_and_degrees() -> None:
    assert catalogue_grid_id("Copernicus_DSM_10_S10_00_W077_00") == "S10_W077"
    assert catalogue_grid_id("Copernicus_DSM_10_N00_00_E004_00") == "N00_E004"


def test_geocells_for_small_circle_returns_containing_cell() -> None:
    assert geocells_for_circle(174.5, -39.5, 100, 0.5) == ["Copernicus_DSM_10_S40_00_E174_00"]


def test_geocell_circle_is_sampled_at_half_degree_intervals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    azimuths: list[float] = []

    class RecordingGeod:
        @staticmethod
        def fwd(
            longitude: float,
            latitude: float,
            azimuth: float,
            _radius_m: float,
        ) -> tuple[float, float, float]:
            azimuths.append(azimuth)
            return longitude, latitude, 0

    monkeypatch.setattr(s3_tiles, "WGS84_GEOD", RecordingGeod())

    geocells_for_circle(174.5, -39.5, 100, 0.5)

    assert azimuths == [half_degree / 2 for half_degree in range(360 * 2)]


def test_geocells_cover_a_degree_boundary() -> None:
    cells = geocells_for_circle(174.0, -39.0, 1000, 0.5)
    assert set(cells) == {
        "Copernicus_DSM_10_S40_00_E173_00",
        "Copernicus_DSM_10_S40_00_E174_00",
        "Copernicus_DSM_10_S39_00_E173_00",
        "Copernicus_DSM_10_S39_00_E174_00",
    }


def test_object_key_matches_dged_layout() -> None:
    assert object_key_for_geocell(TILE_ID, "/eodata/product/") == (
        "product/Copernicus_DSM_10_S40_00_E174_00/DEM/Copernicus_DSM_10_S40_00_E174_00_DEM.tif"
    )


def test_catalogue_response_selects_only_glo30_dged_product_prefixes() -> None:
    payload = {
        "value": [
            {
                "Name": PRODUCT_NAME,
                "S3Path": f"/eodata/{PRODUCT_PREFIX}",
            },
            {
                "Name": "unrelated",
                "S3Path": "/eodata/other-product",
            },
        ]
    }
    assert glo30_product_prefixes(payload) == [PRODUCT_PREFIX]

    with pytest.raises(ValueError, match="invalid response"):
        glo30_product_prefixes({"value": {}})

    with pytest.raises(ValueError, match="none matched the supported GLO-30"):
        glo30_product_prefixes({"value": [{"Name": "unrelated", "S3Path": "/eodata/other"}]})


def test_s3_listing_returns_matching_dem_suffix() -> None:
    s3_client = FakeS3Client(["unrelated.tif", OBJECT_KEY])
    assert find_dem_object(s3_client, "eodata", [PRODUCT_PREFIX], TILE_ID) == OBJECT_KEY
    assert s3_client.list_calls == [
        (
            "eodata",
            f"{PRODUCT_PREFIX}/",
        )
    ]


@pytest.mark.asyncio
async def test_tile_service_downloads_then_reuses_cache(tmp_path: Path) -> None:
    repository = InMemoryTileRepository()
    s3_client = FakeS3Client()
    settings = Settings(
        _env_file=None,
        tile_cache_dir=tmp_path,
        data_dir=tmp_path,
        glo30_s3_prefix="product",
    )
    service = S3TileService(repository, settings, s3_client)

    first = await service.get_tiles(174.5, -39.5, 100)
    second = await service.get_tiles(174.5, -39.5, 100)

    assert first == second
    assert first[0].read_bytes() == b"fake-geotiff"
    assert len(s3_client.calls) == 1
    cached = next(iter(repository.tiles.values()))
    assert cached.last_used_at <= datetime.now(UTC)


@pytest.mark.asyncio
async def test_tile_service_moves_file_work_to_threads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryTileRepository()
    expired_path = tmp_path / "expired.tif"
    expired_path.write_bytes(b"expired")
    expired_tile = CachedTile(
        tile_id="expired",
        object_key="expired",
        file_path=str(expired_path),
        last_used_at=datetime.now(UTC) - timedelta(days=2),
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    repository.tiles[expired_tile.tile_id] = expired_tile
    calls: list[str] = []
    original_to_thread = asyncio.to_thread

    async def recording_to_thread(function: Any, /, *args: Any, **kwargs: Any) -> Any:
        calls.append(function.__name__)
        return await original_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(s3_tiles.asyncio, "to_thread", recording_to_thread)
    s3_client = FakeS3Client()
    monkeypatch.setattr(s3_tiles.boto3, "client", lambda *_args, **_kwargs: s3_client)
    settings = Settings(
        _env_file=None,
        tile_cache_dir=tmp_path,
        data_dir=tmp_path,
        glo30_s3_prefix="product",
        s3_access_key="access-key",
        s3_secret_key="secret-key",
    )
    service = S3TileService(repository, settings)

    await service.get_tiles(174.5, -39.5, 100)
    await service.get_tiles(174.5, -39.5, 100)

    assert "_create_s3_client" in calls
    assert "_download_file" in calls
    assert "is_file" in calls
    assert "unlink" in calls
    assert not expired_path.exists()


@pytest.mark.asyncio
async def test_restricted_tile_is_rejected_before_catalogue_or_s3_access(tmp_path: Path) -> None:
    s3_client = FakeS3Client()
    settings = Settings(
        _env_file=None,
        tile_cache_dir=tmp_path,
        glo30_s3_prefix="product",
    )
    service = S3TileService(InMemoryTileRepository(), settings, s3_client)

    with pytest.raises(DemCoverageError) as error:
        await service.get_tiles(44.75263893480411, 40.11535693795821, 100)

    assert str(error.value) == GLO30_TILE_UNAVAILABLE_MESSAGE
    assert error.value.code == GLO30_TILE_UNAVAILABLE_CODE
    assert error.value.context == {
        "tile_id": RESTRICTED_TILE_ID,
        "reason": "tile_restricted",
    }
    assert error.value.log_detail == f"Restricted GLO-30 tile(s): {RESTRICTED_TILE_ID}"
    assert s3_client.calls == []


@pytest.mark.asyncio
async def test_missing_catalogue_product_is_reported_as_unavailable(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"value": []}))
    settings = Settings(_env_file=None, tile_cache_dir=tmp_path, glo30_s3_prefix=None)

    async with httpx.AsyncClient(transport=transport) as client:
        service = S3TileService(
            InMemoryTileRepository(),
            settings,
            FakeS3Client(),
            client,
        )
        with pytest.raises(DemCoverageError) as error:
            await service.get_tiles(174.5, -39.5, 100)

    assert str(error.value) == GLO30_TILE_UNAVAILABLE_MESSAGE
    assert error.value.code == GLO30_TILE_UNAVAILABLE_CODE
    assert error.value.context == {
        "tile_id": TILE_ID,
        "reason": "catalogue_product_not_found",
    }
    assert error.value.log_detail == (
        "No GLO-30 catalogue product covers Copernicus_DSM_10_S40_00_E174_00"
    )


@pytest.mark.asyncio
async def test_missing_adjacent_tile_uses_cached_zero_elevation_tile(tmp_path: Path) -> None:
    observer_tile_id = "Copernicus_DSM_10_S08_00_E131_00"
    available_tile_ids = {
        observer_tile_id,
        "Copernicus_DSM_10_S07_00_E131_00",
        "Copernicus_DSM_10_S07_00_E132_00",
    }
    missing_tile_id = "Copernicus_DSM_10_S08_00_E132_00"
    grid_ids = {
        catalogue_grid_id(tile_id): tile_id for tile_id in available_tile_ids | {missing_tile_id}
    }
    queried_grid_ids: list[str] = []

    def catalogue_response(request: httpx.Request) -> httpx.Response:
        query_filter = request.url.params["$filter"]
        grid_id = next(grid_id for grid_id in grid_ids if grid_id in query_filter)
        queried_grid_ids.append(grid_id)
        if grid_ids[grid_id] in available_tile_ids:
            return httpx.Response(
                200,
                json={"value": [{"Name": PRODUCT_NAME, "S3Path": f"/eodata/{PRODUCT_PREFIX}"}]},
            )
        return httpx.Response(200, json={"value": []})

    repository = InMemoryTileRepository()
    settings = Settings(_env_file=None, tile_cache_dir=tmp_path, glo30_s3_prefix=None)
    s3_client = FakeS3Client(
        [f"{PRODUCT_PREFIX}/{tile_id}/DEM/{tile_id}_DEM.tif" for tile_id in available_tile_ids]
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(catalogue_response)) as client:
        service = S3TileService(repository, settings, s3_client, client)
        first = await service.get_tiles(131.97368749999998, -7.0015625, 3000)
        second = await service.get_tiles(131.97368749999998, -7.0015625, 3000)

    assert first == second
    assert queried_grid_ids == ["S08_E131", "S07_E131", "S07_E132", "S08_E132"]
    assert [path.name for path in first] == [
        f"{observer_tile_id}_DEM.tif",
        "Copernicus_DSM_10_S07_00_E131_00_DEM.tif",
        "Copernicus_DSM_10_S07_00_E132_00_DEM.tif",
        f"{missing_tile_id}_DEM.tif",
    ]
    assert repository.tiles[missing_tile_id].object_key == ZERO_ELEVATION_OBJECT_KEY
    with rasterio.open(first[-1]) as zero_tile:
        assert zero_tile.crs == rasterio.CRS.from_epsg(4326)
        assert tuple(zero_tile.bounds) == (132.0, -8.0, 133.0, -7.0)
        assert zero_tile.read(1).tolist() == [[0.0]]


@pytest.mark.asyncio
async def test_unmatched_nonempty_catalogue_has_layout_error(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"value": [{"Name": "new-layout", "S3Path": "/eodata/new-layout"}]},
        )
    )
    settings = Settings(_env_file=None, tile_cache_dir=tmp_path, glo30_s3_prefix=None)

    async with httpx.AsyncClient(transport=transport) as client:
        service = S3TileService(InMemoryTileRepository(), settings, FakeS3Client(), client)
        with pytest.raises(TileDownloadError) as error:
            await service.get_tiles(174.5, -39.5, 100)

    assert str(error.value) == GLO30_CATALOGUE_LAYOUT_UNSUPPORTED_MESSAGE
    assert error.value.code == GLO30_CATALOGUE_LAYOUT_UNSUPPORTED_CODE
    assert error.value.context == {"tile_id": TILE_ID}
    assert error.value.log_detail is not None
    assert "1 product(s)" in error.value.log_detail
    assert "name and S3-path patterns" in error.value.log_detail


@pytest.mark.asyncio
async def test_missing_discovered_s3_object_identifies_tile(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"value": [{"Name": PRODUCT_NAME, "S3Path": f"/eodata/{PRODUCT_PREFIX}"}]},
        )
    )
    settings = Settings(_env_file=None, tile_cache_dir=tmp_path, glo30_s3_prefix=None)

    async with httpx.AsyncClient(transport=transport) as client:
        service = S3TileService(InMemoryTileRepository(), settings, FakeS3Client(), client)
        with pytest.raises(DemCoverageError) as error:
            await service.get_tiles(174.5, -39.5, 100)

    assert str(error.value) == GLO30_TILE_UNAVAILABLE_MESSAGE
    assert error.value.code == GLO30_TILE_UNAVAILABLE_CODE
    assert error.value.context == {"tile_id": TILE_ID, "reason": "dem_object_not_found"}
    assert error.value.log_detail is not None
    assert TILE_ID in error.value.log_detail


@pytest.mark.asyncio
async def test_missing_direct_s3_object_is_reported_as_unavailable(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        tile_cache_dir=tmp_path,
        glo30_s3_prefix="product",
    )
    service = S3TileService(InMemoryTileRepository(), settings, MissingS3Client())

    with pytest.raises(DemCoverageError) as error:
        await service.get_tiles(174.5, -39.5, 100)

    assert str(error.value) == GLO30_TILE_UNAVAILABLE_MESSAGE
    assert error.value.code == GLO30_TILE_UNAVAILABLE_CODE
    assert error.value.context == {"tile_id": TILE_ID, "reason": "dem_object_not_found"}
    assert error.value.log_detail is not None
    assert object_key_for_geocell(TILE_ID, "product") in error.value.log_detail


@pytest.mark.asyncio
async def test_tile_service_discovers_live_layout_then_caches_object_key(tmp_path: Path) -> None:
    repository = InMemoryTileRepository()
    s3_client = FakeS3Client(["wrong-file.tif", OBJECT_KEY])
    requests: list[httpx.Request] = []

    def catalogue_response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "Name": PRODUCT_NAME,
                        "S3Path": f"/eodata/{PRODUCT_PREFIX}",
                    }
                ]
            },
        )

    settings = Settings(
        _env_file=None,
        tile_cache_dir=tmp_path,
        data_dir=tmp_path,
        glo30_s3_prefix=None,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(catalogue_response)) as client:
        service = S3TileService(repository, settings, s3_client, client)
        first = await service.get_tiles(174.5, -39.5, 100)
        second = await service.get_tiles(174.5, -39.5, 100)

    assert first == second
    assert len(requests) == 1
    assert "datasetFull" in requests[0].url.params["$filter"]
    assert "gridId" in requests[0].url.params["$filter"]
    assert "S40_E174" in requests[0].url.params["$filter"]
    assert "Collection/Name" not in requests[0].url.params["$filter"]
    assert requests[0].url.params["$select"] == "Name,S3Path"
    assert len(s3_client.calls) == 1
    assert repository.tiles[TILE_ID].object_key == OBJECT_KEY


@pytest.mark.asyncio
async def test_invalid_catalogue_response_becomes_tile_error(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, tile_cache_dir=tmp_path, glo30_s3_prefix=None)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"value": {}}))
    async with httpx.AsyncClient(transport=transport) as client:
        service = S3TileService(InMemoryTileRepository(), settings, FakeS3Client(), client)
        with pytest.raises(TileDownloadError) as error:
            await service.get_tiles(174.5, -39.5, 100)

    assert str(error.value) == COPERNICUS_UNAVAILABLE_MESSAGE
    assert error.value.log_detail == (
        "Copernicus catalogue returned invalid data for "
        "Copernicus_DSM_10_S40_00_E174_00: Copernicus catalogue returned an invalid response"
    )


@pytest.mark.asyncio
async def test_catalogue_timeout_is_reported_explicitly(tmp_path: Path) -> None:
    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("catalogue did not respond")

    settings = Settings(_env_file=None, tile_cache_dir=tmp_path, glo30_s3_prefix=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        service = S3TileService(InMemoryTileRepository(), settings, FakeS3Client(), client)
        with pytest.raises(CopernicusTimeoutError) as error:
            await service.get_tiles(174.5, -39.5, 100)

    assert str(error.value) == COPERNICUS_TIMEOUT_MESSAGE
    assert error.value.log_detail is not None
    assert "catalogue timed out" in error.value.log_detail
    assert "ReadTimeout" in error.value.log_detail


@pytest.mark.asyncio
async def test_catalogue_rejection_is_reported_as_site_configuration_error(
    tmp_path: Path,
) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(403))
    settings = Settings(_env_file=None, tile_cache_dir=tmp_path, glo30_s3_prefix=None)
    async with httpx.AsyncClient(transport=transport) as client:
        service = S3TileService(InMemoryTileRepository(), settings, FakeS3Client(), client)
        with pytest.raises(CopernicusConfigurationError) as error:
            await service.get_tiles(174.5, -39.5, 100)

    assert str(error.value) == COPERNICUS_CONFIGURATION_MESSAGE
    assert error.value.log_detail == (
        "Copernicus catalogue rejected access while resolving "
        "Copernicus_DSM_10_S40_00_E174_00 (HTTP 403)"
    )


class RejectedS3Client(FakeS3Client):
    def download_file(self, bucket: str, key: str, filename: str) -> None:
        raise ClientError(
            {
                "Error": {
                    "Code": "InvalidAccessKeyId",
                    "Message": "The supplied access key is not valid",
                },
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "GetObject",
        )


class TimedOutS3Client(FakeS3Client):
    def download_file(self, bucket: str, key: str, filename: str) -> None:
        raise ReadTimeoutError(endpoint_url="https://example.invalid")


@pytest.mark.asyncio
async def test_s3_credential_rejection_is_explicit_in_log_detail_only(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, tile_cache_dir=tmp_path, glo30_s3_prefix="product")
    service = S3TileService(InMemoryTileRepository(), settings, RejectedS3Client())

    with pytest.raises(CopernicusConfigurationError) as error:
        await service.get_tiles(174.5, -39.5, 100)

    assert str(error.value) == COPERNICUS_CONFIGURATION_MESSAGE
    assert "access key" not in str(error.value).lower()
    assert error.value.log_detail == (
        "Copernicus S3 download failed for Copernicus_DSM_10_S40_00_E174_00 "
        "(HTTP 403, code=InvalidAccessKeyId)"
    )


@pytest.mark.asyncio
async def test_s3_timeout_is_reported_explicitly(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, tile_cache_dir=tmp_path, glo30_s3_prefix="product")
    service = S3TileService(InMemoryTileRepository(), settings, TimedOutS3Client())

    with pytest.raises(CopernicusTimeoutError) as error:
        await service.get_tiles(174.5, -39.5, 100)

    assert str(error.value) == COPERNICUS_TIMEOUT_MESSAGE
    assert error.value.log_detail is not None
    assert "S3 download timed out" in error.value.log_detail
    assert "ReadTimeoutError" in error.value.log_detail


def test_fake_repository_satisfies_service_protocol_at_runtime() -> None:
    # This test intentionally keeps the fake's public surface obvious as the service evolves.
    repository: Any = InMemoryTileRepository()
    assert callable(repository.get_by_tile_id)
