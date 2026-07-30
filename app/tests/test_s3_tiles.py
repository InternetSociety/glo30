from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.exceptions import TileDownloadError
from app.models.cached_tile import CachedTile
from app.services.s3_tiles import (
    S3TileService,
    ccm_prefix_for_product,
    copernicus_geocell,
    find_dem_object,
    geocell_center,
    geocells_for_circle,
    glo30_product_names,
    object_key_for_geocell,
)

PRODUCT_NAME = "DEM1_SAR_DGE_30_20101226T173648_20140818T173725_ADS_000000_ri2b"
TILE_ID = "Copernicus_DSM_10_S40_00_E174_00"
OBJECT_KEY = (
    "CCM/COP-DEM_GLO-30-DGED/SAR_DGE_30_A4AD/2010/12/26/"
    f"{PRODUCT_NAME}_abc123/{TILE_ID}/DEM/{TILE_ID}_DEM.tif"
)


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


def test_copernicus_geocell_uses_southwest_degree() -> None:
    assert copernicus_geocell(-40, 174) == "Copernicus_DSM_10_S40_00_E174_00"
    assert copernicus_geocell(0, -1) == "Copernicus_DSM_10_N00_00_W001_00"


def test_geocell_center_parses_hemispheres() -> None:
    assert geocell_center("Copernicus_DSM_10_S40_00_E174_00") == (174.5, -39.5)
    assert geocell_center("Copernicus_DSM_10_N00_00_W001_00") == (-0.5, 0.5)


def test_geocells_for_small_circle_returns_containing_cell() -> None:
    assert geocells_for_circle(174.5, -39.5, 100) == ["Copernicus_DSM_10_S40_00_E174_00"]


def test_geocells_cover_a_degree_boundary() -> None:
    cells = geocells_for_circle(174.0, -39.0, 1000)
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


def test_ccm_prefix_uses_product_acquisition_date() -> None:
    assert ccm_prefix_for_product(PRODUCT_NAME) == (
        f"CCM/COP-DEM_GLO-30-DGED/SAR_DGE_30_A4AD/2010/12/26/{PRODUCT_NAME}_"
    )


def test_catalogue_response_selects_only_glo30_dged_products() -> None:
    payload = {
        "value": [
            {
                "Name": PRODUCT_NAME,
                "S3Path": f"/eodata/auxdata/CopDEM/COP-DEM_GLO-30-DGED/{PRODUCT_NAME}.DEM",
            },
            {
                "Name": "unrelated",
                "S3Path": "/eodata/other-product",
            },
        ]
    }
    assert glo30_product_names(payload) == [PRODUCT_NAME]

    with pytest.raises(ValueError, match="invalid response"):
        glo30_product_names({"value": {}})


def test_s3_listing_returns_matching_dem_suffix() -> None:
    s3_client = FakeS3Client(["unrelated.tif", OBJECT_KEY])
    assert find_dem_object(s3_client, "eodata", [PRODUCT_NAME], TILE_ID) == OBJECT_KEY
    assert s3_client.list_calls == [
        (
            "eodata",
            f"CCM/COP-DEM_GLO-30-DGED/SAR_DGE_30_A4AD/2010/12/26/{PRODUCT_NAME}_",
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
                        "S3Path": (
                            f"/eodata/auxdata/CopDEM/COP-DEM_GLO-30-DGED/{PRODUCT_NAME}.DEM"
                        ),
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
    assert "POINT (174.5 -39.5)" in requests[0].url.params["$filter"]
    assert requests[0].url.params["$select"] == "Name,S3Path"
    assert len(s3_client.calls) == 1
    assert repository.tiles[TILE_ID].object_key == OBJECT_KEY


@pytest.mark.asyncio
async def test_invalid_catalogue_response_becomes_tile_error(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, tile_cache_dir=tmp_path, glo30_s3_prefix=None)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"value": {}}))
    async with httpx.AsyncClient(transport=transport) as client:
        service = S3TileService(InMemoryTileRepository(), settings, FakeS3Client(), client)
        with pytest.raises(TileDownloadError, match="Unable to query"):
            await service.get_tiles(174.5, -39.5, 100)


def test_fake_repository_satisfies_service_protocol_at_runtime() -> None:
    # This test intentionally keeps the fake's public surface obvious as the service evolves.
    repository: Any = InMemoryTileRepository()
    assert callable(repository.get_by_tile_id)
