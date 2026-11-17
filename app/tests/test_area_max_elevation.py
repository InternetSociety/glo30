"""Area maxima use the same native cells as horizon v2, without horizon decisions."""

import asyncio
import hashlib

import pytest
from shapely.geometry import MultiPolygon, box, mapping

from app.config import settings
from app.services.s3_tiles import S3TileService, UnavailableTile
from app.tests.test_terrain_api import AUTH, TILE, raster
from app.tests.test_terrain_api import terrain as terrain_fixture

terrain = terrain_fixture
URL = "/api/v1/area-max-elevation"


def area(geometry=None):
    return {"geometry": mapping(geometry if geometry is not None else box(0.1, 0.1, 0.9, 0.9))}


async def test_maximum_and_provenance(client, create_test_user, terrain, tmp_path):
    await create_test_user()
    values = [[1] * 8 for _ in range(8)]
    values[4][4] = 959.375
    values[0][0] = 5000  # Higher, but outside the requested polygon.
    path = raster(terrain, tmp_path, values, step=1 / 8)
    response = await client.post(URL, headers=AUTH, json=area(box(0.2, 0.2, 0.8, 0.8)))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["max_elevation_m"] == 959.375
    assert body["coordinates"] == [0.5625, 0.4375]
    assert body["coverage_complete"] is True
    assert body["evaluated_cells"] > 0
    assert body["unavailable_cells"] == 0
    assert body["provenance"]["tiles"] == [
        {
            "tile_id": TILE,
            "product_id": "test/product",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    ]
    assert body["provenance"]["processor_version"] == "area-max-elevation-v1"


async def test_hole_and_ties(client, create_test_user, terrain, tmp_path):
    await create_test_user()
    raster(terrain, tmp_path, [[9, 9, 9], [9, 100, 9], [9, 9, 9]], step=1 / 3)
    polygon = box(0.01, 0.01, 0.99, 0.99).difference(box(0.32, 0.32, 0.68, 0.68))
    response = await client.post(URL, headers=AUTH, json=area(polygon))
    assert response.status_code == 200, response.text
    assert response.json()["max_elevation_m"] == 9
    assert response.json()["coordinates"] == pytest.approx([1 / 6, 1 / 6])
    assert response.json()["evaluated_cells"] == 8


async def test_sliver_returns_containing_cell_centre(client, create_test_user, terrain, tmp_path):
    await create_test_user()
    raster(terrain, tmp_path, [[-5, -4], [-2.5, -3]])
    response = await client.post(URL, headers=AUTH, json=area(box(0.1, 0.1, 0.2, 0.11)))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["max_elevation_m"] == -2.5
    assert body["coordinates"] == [0.25, 0.25]
    assert body["evaluated_cells"] == 1


@pytest.mark.parametrize(
    "values,expected,status",
    [
        ([[10, -9999], [-9999, -9999]], 10, "ok"),
        ([[-9999, -9999], [-9999, -9999]], None, "unavailable"),
        ([[float("nan"), -9999], [-9999, -9999]], None, "unavailable"),
    ],
)
async def test_nodata_and_empty_maximum(
    client, create_test_user, terrain, tmp_path, values, expected, status
):
    await create_test_user()
    raster(terrain, tmp_path, values)
    response = await client.post(URL, headers=AUTH, json=area())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == status
    assert body["max_elevation_m"] == expected
    assert body["coverage_complete"] is False
    assert body["unavailable_cells"] > 0
    if expected is None:
        assert body["coordinates"] is None
        assert body["evaluated_cells"] == 0


@pytest.mark.parametrize(
    "reason,expected",
    [("catalogue_product_not_found", 0), ("dem_object_not_found", 0), ("tile_restricted", None)],
)
async def test_sea_and_restricted(client, create_test_user, monkeypatch, reason, expected):
    await create_test_user()

    async def acquire(self, tile_ids):
        return {tile_id: UnavailableTile(tile_id, reason) for tile_id in tile_ids}

    monkeypatch.setattr(S3TileService, "get_native_sources", acquire)
    response = await client.post(URL, headers=AUTH, json=area(box(0.1, 0.1, 0.101, 0.101)))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["max_elevation_m"] == expected
    assert body["coverage_complete"] is (expected is not None)
    assert body["provenance"]["sea_level_assumed_tiles"] == ([TILE] if expected is not None else [])
    assert body["provenance"]["tiles"] == []


async def test_authentication(client, create_test_user, terrain):
    assert (await client.post(URL, json=area())).status_code == 401
    await create_test_user(is_active=False)
    assert (await client.post(URL, headers=AUTH, json=area())).status_code == 403


@pytest.mark.parametrize(
    "geometry",
    [
        {"type": "Point", "coordinates": [0, 0]},
        {"type": "Polygon", "coordinates": []},
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [0, 1], [1, 0], [0, 0]]]},
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 1]]]},
        {"type": "Polygon", "coordinates": [[[179, 0], [-179, 0], [-179, 1], [179, 0]]]},
    ],
)
async def test_geometry_validation(client, create_test_user, terrain, geometry):
    await create_test_user()
    assert (await client.post(URL, headers=AUTH, json={"geometry": geometry})).status_code == 422


async def test_own_limit_and_no_geodesic_work(
    client, create_test_user, terrain, tmp_path, monkeypatch
):
    from app.services import horizon_sampling

    await create_test_user()
    raster(terrain, tmp_path, [[1, 2], [3, 4]])
    monkeypatch.setattr(settings, "area_horizon_max_cells", 1)

    def forbidden(*args, **kwargs):
        raise AssertionError("No geodesic calculation is needed for a maximum")

    monkeypatch.setattr(horizon_sampling, "batch_distance_bounds", forbidden)
    response = await client.post(URL, headers=AUTH, json=area())
    assert response.status_code == 200, response.text
    monkeypatch.setattr(settings, "area_max_elevation_max_cells", 1)
    response = await client.post(URL, headers=AUTH, json=area())
    assert response.status_code == 422
    assert response.json()["code"] == "area_max_elevation_limit"


async def test_deadline(client, create_test_user, terrain, monkeypatch):
    await create_test_user()

    async def slow(self, tile_ids):
        await asyncio.Event().wait()

    monkeypatch.setattr(S3TileService, "get_native_sources", slow)
    monkeypatch.setattr(settings, "area_max_elevation_timeout_seconds", 0.02)
    response = await client.post(URL, headers=AUTH, json=area())
    assert response.status_code == 504


async def test_multipolygon_counts_once(client, create_test_user, terrain, tmp_path):
    await create_test_user()
    raster(terrain, tmp_path, [[1, 2], [3, 4]])
    geometry = MultiPolygon([box(0.1, 0.1, 0.11, 0.11), box(0.2, 0.2, 0.21, 0.21)])
    response = await client.post(URL, headers=AUTH, json=area(geometry))
    assert response.status_code == 200, response.text
    assert response.json()["evaluated_cells"] == 1


async def test_published_swagger_example_returns_200(client, create_test_user, terrain, tmp_path):
    await create_test_user()
    raster(
        terrain,
        tmp_path,
        [[900, 900], [900, 900]],
        tile_id="Copernicus_DSM_10_S38_00_E175_00",
        west=175,
        north=-37,
    )
    raster(
        terrain,
        tmp_path,
        [[800, 800], [800, 800]],
        tile_id="Copernicus_DSM_10_S39_00_E175_00",
        west=175,
        north=-38,
    )
    document = (await client.get("/openapi.json", headers=AUTH)).json()
    reference = document["paths"][URL]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    example = document["components"]["schemas"][reference.rsplit("/", 1)[-1]]["example"]
    response = await client.post(URL, headers=AUTH, json=example)
    assert response.status_code == 200, response.text
    assert response.json()["max_elevation_m"] == 900


async def test_raster_worker_yields_and_stops_after_deadline(
    client, create_test_user, terrain, tmp_path, monkeypatch
):
    import threading

    from app.services import area_max_elevation

    await create_test_user()
    raster(terrain, tmp_path, [[1, 2], [3, 4]])
    entered, release, exited = asyncio.Event(), threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = area_max_elevation.Maximum.consume

    def paused(self, *args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        try:
            if not release.wait(2):
                raise AssertionError("Event loop did not release the raster worker")
            return original(self, *args, **kwargs)
        finally:
            exited.set()

    monkeypatch.setattr(area_max_elevation.Maximum, "consume", paused)
    monkeypatch.setattr(settings, "area_max_elevation_timeout_seconds", 0.1)
    request = asyncio.create_task(client.post(URL, headers=AUTH, json=area()))
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            health = await client.get("/api/v1/health", headers=AUTH)
            assert health.status_code == 200
            response = await request
            assert response.status_code == 504
            assert response.json()["code"] == "area_max_elevation_timeout"
    finally:
        release.set()
        await request
    assert await asyncio.to_thread(exited.wait, 1)
