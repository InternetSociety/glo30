"""Native terrain contracts exercised through the real API and raster processors."""

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box, mapping

from app.config import settings
from app.services.s3_tiles import S3TileService

AUTH = {"Authorization": "Bearer test-bearer-token"}
TILE = "Copernicus_DSM_10_N00_00_E000_00"


@pytest.fixture
async def terrain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    paths: dict[str, Path] = {}

    async def acquire(self: S3TileService, tile_ids: list[str]) -> dict[str, Any]:
        from app.services.s3_tiles import NativeTile, UnavailableTile

        return {
            tile_id: NativeTile(tile_id, "test/product", paths[tile_id])
            if tile_id in paths
            else UnavailableTile(tile_id, "coverage_unavailable")
            for tile_id in tile_ids
        }

    monkeypatch.setattr(S3TileService, "get_native_sources", acquire)
    return paths


def raster(
    paths: dict[str, Path],
    directory: Path,
    values: list[list[float]],
    tile_id: str = TILE,
    west: float = 0,
    north: float = 1,
    step: float = 0.5,
    point_registered: bool = False,
) -> Path:
    path = directory / f"{tile_id}.tif"
    data = np.array(values, dtype="float32")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=data.shape[1],
        height=data.shape[0],
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        nodata=-9999,
        transform=from_origin(west, north, step, step),
    ) as dst:
        dst.write(data, 1)
        if point_registered:
            dst.update_tags(AREA_OR_POINT="Point")
    paths[tile_id] = path
    return path


def horizon(geometry: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return {
        "observer_coordinates": [0, 0.25],
        "observer_antenna_height_asl_m": 0,
        "target_height_agl_m": 0,
        "geometry": geometry,
        **overrides,
    }


@pytest.mark.parametrize(
    "endpoint,body",
    [
        ("elevations", {"coordinates": [[0.1, 0.1]]}),
        ("area-horizon", horizon({"type": "Point", "coordinates": [0.1, 0.1]})),
    ],
)
async def test_terrain_requires_active_account(client, create_test_user, terrain, endpoint, body):
    response = await client.post(f"/api/v1/{endpoint}", json=body)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    await create_test_user(is_active=False)
    response = await client.post(f"/api/v1/{endpoint}", json=body, headers=AUTH)
    assert response.status_code == 403


async def test_elevations_native_values_order_and_provenance(
    client,
    create_test_user,
    terrain,
    tmp_path,
):
    await create_test_user()
    path = raster(terrain, tmp_path, [[12.5, -2], [0, -9999]])
    points = [[0.8, 0.8], [0.2, 0.2], [0.8, 0.2], [0.2, 0.8], [0.8, 0.8], [1.2, 0.2]]
    response = await client.post("/api/v1/elevations", json={"coordinates": points}, headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [r["coordinates"] for r in body["results"]] == points
    assert [r["elevation_m"] for r in body["results"]] == [-2, 0, None, 12.5, -2, None]
    assert [r["status"] for r in body["results"]] == [
        "ok",
        "ok",
        "unavailable",
        "ok",
        "ok",
        "unavailable",
    ]
    assert body["results"][2]["reason"] == "dem_unavailable"
    assert body["provenance"]["sampling"] == "native_cell"
    assert body["provenance"]["tiles"] == [
        {
            "tile_id": TILE,
            "product_id": "test/product",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    ]


@pytest.mark.parametrize(
    "coordinates", [[], [[0, 0]] * 1001, [[181, 0]], [[0, -91]], [[0]], [[0, 0, 0]]]
)
async def test_elevation_validation(client, create_test_user, terrain, coordinates):
    await create_test_user()
    response = await client.post(
        "/api/v1/elevations", json={"coordinates": coordinates}, headers=AUTH
    )
    assert response.status_code == 422


async def test_elevations_geocell_and_native_boundaries(
    client, create_test_user, terrain, tmp_path
):
    await create_test_user()
    raster(terrain, tmp_path, [[10, 20], [30, 40]])
    raster(
        terrain, tmp_path, [[50, 60], [70, 80]], tile_id="Copernicus_DSM_10_N00_00_E001_00", west=1
    )
    raster(
        terrain, tmp_path, [[90, 90], [90, 90]], tile_id="Copernicus_DSM_10_N01_00_E000_00", north=2
    )
    response = await client.post(
        "/api/v1/elevations",
        headers=AUTH,
        json={"coordinates": [[0.5, 0.75], [1, 0.25], [0.25, 1]]},
    )
    assert response.status_code == 200, response.text
    assert [r["elevation_m"] for r in response.json()["results"]] == [20, 70, 90]


async def test_horizon_keeps_distance_and_height_together(
    client, create_test_user, terrain, tmp_path
):
    await create_test_user()
    # Nearest cell is flat at 33 km; farther cell starts at 55 km and reaches only 41.2 km.
    raster(terrain, tmp_path, [[0, 100], [0, 100]], step=0.5)
    geometry = mapping(box(0.3, 0.2, 0.9, 0.3))
    response = await client.post("/api/v1/area-horizon", headers=AUTH, json=horizon(geometry))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "beyond_horizon"
    assert body["coverage_complete"] is True
    assert body["evaluated_cells"] == 2
    assert body["evidence"]["elevation_m"] == 100
    assert body["evidence"]["coordinates"][0] == pytest.approx(0.5, abs=1e-6)


@pytest.mark.parametrize("high,expected", [(100, "beyond_horizon"), (10000, "within_horizon")])
async def test_farther_elevated_cell_can_be_witness(
    client,
    create_test_user,
    terrain,
    tmp_path,
    high,
    expected,
):
    await create_test_user()
    raster(terrain, tmp_path, [[0, high], [0, high]])
    response = await client.post(
        "/api/v1/area-horizon",
        headers=AUTH,
        json=horizon(mapping(box(0.3, 0.2, 0.9, 0.3)), observer_coordinates=[0, 0.25]),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == expected
    if high == 10000:
        assert body["evidence"]["elevation_m"] == 10000
        assert body["evidence"]["coordinates"][0] == pytest.approx(0.5, abs=1e-6)


@pytest.mark.parametrize("height,expected", [(10000, "within_horizon"), (0, "indeterminate")])
async def test_partial_dem_never_proves_beyond(
    client, create_test_user, terrain, tmp_path, height, expected
):
    await create_test_user()
    raster(terrain, tmp_path, [[height, -9999], [height, -9999]])
    response = await client.post(
        "/api/v1/area-horizon", headers=AUTH, json=horizon(mapping(box(0.3, 0.2, 0.9, 0.3)))
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == expected
    assert body["coverage_complete"] is False
    assert body["evaluated_cells"] == 1
    assert body["unavailable_cells"] == 1
    assert body["evidence"] is not None


async def test_missing_dem_returns_indeterminate(client, create_test_user, terrain):
    await create_test_user()
    response = await client.post(
        "/api/v1/area-horizon",
        headers=AUTH,
        json=horizon({"type": "Point", "coordinates": [0.1, 0.2]}),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "indeterminate"
    assert body["reason"] == "incomplete_dem"
    assert body["unavailable_cells"] == 1
    assert body["evidence"] is None
    assert body["provenance"]["tiles"] == []


async def test_polygon_hole_excludes_only_blocking_cell(
    client, create_test_user, terrain, tmp_path
):
    await create_test_user()
    raster(terrain, tmp_path, [[0, 0, 0], [0, 10000, 0], [0, 0, 0]], step=1 / 3)
    outer = box(0.1, 0.1, 0.9, 0.9)
    # Hole strictly encloses the complete elevated cell.
    shape = outer.difference(box(0.32, 0.32, 0.68, 0.68))
    response = await client.post("/api/v1/area-horizon", headers=AUTH, json=horizon(mapping(shape)))
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "beyond_horizon"
    assert response.json()["evaluated_cells"] == 8


async def test_observer_inside_and_zero_horizon_equality(
    client, create_test_user, terrain, tmp_path
):
    await create_test_user()
    raster(terrain, tmp_path, [[0, 0], [0, 0]])
    response = await client.post(
        "/api/v1/area-horizon",
        headers=AUTH,
        json=horizon(mapping(box(0, 0, 0.4, 0.4)), observer_antenna_height_asl_m=-100),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "within_horizon"
    assert body["observer_antenna_height_asl_m"] == 0
    assert body["evidence"]["distance_km"] == 0
    assert body["evidence"]["horizon_km"] == 0


@pytest.mark.parametrize(
    "geometry",
    [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]}},
        {"type": "Polygon", "coordinates": []},
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [0, 1], [1, 0], [0, 0]]]},
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 1]]]},
        {"type": "Polygon", "coordinates": [[[179, 0], [-179, 0], [-179, 1], [179, 0]]]},
        {"type": "Point", "coordinates": [0, 91]},
        {"type": "MultiPoint", "coordinates": []},
    ],
)
async def test_horizon_rejects_invalid_geometry(client, create_test_user, terrain, geometry):
    await create_test_user()
    response = await client.post("/api/v1/area-horizon", headers=AUTH, json=horizon(geometry))
    assert response.status_code == 422, response.text


async def test_multipoint_ties_and_radius_scaling(client, create_test_user, terrain, tmp_path):
    await create_test_user()
    raster(terrain, tmp_path, [[100, 100], [100, 100]])
    geometry = {"type": "MultiPoint", "coordinates": [[0.4, 0.25], [0.2, 0.25], [0.2, 0.25]]}
    body = horizon(geometry, observer_antenna_height_asl_m=100, effective_earth_radius_factor=4 / 3)
    first = await client.post("/api/v1/area-horizon", headers=AUTH, json=body)
    assert first.status_code == 200, first.text
    result = first.json()
    assert result["evidence"]["coordinates"] == [0.2, 0.25]
    assert result["evaluated_cells"] == 3
    assert result["evidence"]["horizon_km"] == pytest.approx(82.4)
    body["effective_earth_radius_factor"] = 1 / 3
    second = await client.post("/api/v1/area-horizon", headers=AUTH, json=body)
    assert second.json()["evidence"]["horizon_km"] == pytest.approx(41.2)


async def test_configured_limits(client, create_test_user, terrain, monkeypatch):
    await create_test_user()
    monkeypatch.setattr(settings, "elevation_max_batch_points", 1, raising=False)
    response = await client.post(
        "/api/v1/elevations", headers=AUTH, json={"coordinates": [[0, 0], [1, 1]]}
    )
    assert response.status_code == 422
    monkeypatch.setattr(settings, "area_horizon_max_cells", 2, raising=False)
    response = await client.post(
        "/api/v1/area-horizon", headers=AUTH, json=horizon(mapping(box(0.1, 0.1, 0.2, 0.2)))
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "area_horizon_limit"


@pytest.mark.parametrize(
    "endpoint,field,value",
    [
        ("elevations", "coordinates", [[float("nan"), 0]]),
        ("elevations", "coordinates", [[0, float("inf")]]),
        ("area-horizon", "observer_antenna_height_asl_m", float("nan")),
        ("area-horizon", "effective_earth_radius_factor", float("inf")),
        ("area-horizon", "target_height_agl_m", float("-inf")),
        ("area-horizon", "target_height_agl_m", -1),
        ("area-horizon", "effective_earth_radius_factor", 0),
        ("area-horizon", "observer_antenna_height_asl_m", 12001),
    ],
)
async def test_nonfinite_and_numeric_bounds(
    client, create_test_user, terrain, endpoint, field, value
):
    import json

    await create_test_user()
    body = (
        {"coordinates": [[0, 0]]}
        if endpoint == "elevations"
        else horizon({"type": "Point", "coordinates": [0.1, 0.2]})
    )
    body[field] = value
    response = await client.post(
        f"/api/v1/{endpoint}",
        headers={**AUTH, "Content-Type": "application/json"},
        content=json.dumps(body),
    )
    assert response.status_code == 422, response.text


async def test_missing_polygon_cells_and_disconnected_witness(
    client, create_test_user, terrain, tmp_path
):
    await create_test_user()
    raster(terrain, tmp_path, [[10000, 10000], [10000, 10000]])
    from shapely.geometry import MultiPolygon

    geometry = mapping(MultiPolygon([box(0.2, 0.2, 0.21, 0.21), box(1.2, 0.2, 1.201, 0.201)]))
    response = await client.post("/api/v1/area-horizon", headers=AUTH, json=horizon(geometry))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "within_horizon"
    assert not body["coverage_complete"]
    assert body["reason"] == "stopped_at_witness"
    assert body["unavailable_cells"] == 0
    assert body["evaluated_cells"] == 1


async def test_separate_antimeridian_components_are_permitted(client, create_test_user, terrain):
    from shapely.geometry import MultiPolygon

    await create_test_user()
    geometry = mapping(
        MultiPolygon([box(179.1, 0.1, 179.101, 0.101), box(-179.1, 0.1, -179.099, 0.101)])
    )
    response = await client.post("/api/v1/area-horizon", headers=AUTH, json=horizon(geometry))
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "indeterminate"


async def test_point_registration_and_global_clamps(client, create_test_user, terrain, tmp_path):
    await create_test_user()
    # Pixel centres lie on a 0.5-degree posting grid. Floor the actual transform;
    # do not derive the raster origin from the geocell name or round to postings.
    raster(
        terrain,
        tmp_path,
        [[11, 12, 13], [21, 22, 23], [31, 32, 33]],
        west=-0.25,
        north=1.25,
        step=0.5,
        point_registered=True,
    )
    raster(
        terrain,
        tmp_path,
        [[7, 8], [9, 10]],
        tile_id="Copernicus_DSM_10_N89_00_E179_00",
        west=179,
        north=90,
    )
    raster(
        terrain,
        tmp_path,
        [[-7, -8], [-9, -10]],
        tile_id="Copernicus_DSM_10_S90_00_W180_00",
        west=-180,
        north=-89,
    )
    response = await client.post(
        "/api/v1/elevations",
        headers=AUTH,
        json={"coordinates": [[0.49, 0.49], [180, 90], [-180, -90]]},
    )
    assert response.status_code == 200, response.text
    assert [r["elevation_m"] for r in response.json()["results"]] == [22, 8, -9]


@pytest.mark.parametrize("endpoint", ["elevations", "area-horizon"])
@pytest.mark.parametrize(
    "error_kind,expected", [("config", 502), ("timeout", 504), ("network", 502)]
)
async def test_upstream_errors_are_not_missing_terrain(
    client,
    create_test_user,
    monkeypatch,
    endpoint,
    error_kind,
    expected,
):
    from app.exceptions import (
        CopernicusConfigurationError,
        CopernicusTimeoutError,
        TileDownloadError,
    )

    await create_test_user()
    error_type = {
        "config": CopernicusConfigurationError,
        "timeout": CopernicusTimeoutError,
        "network": TileDownloadError,
    }[error_kind]

    async def fail(self, tile_ids):
        raise error_type("Safe upstream error")

    monkeypatch.setattr(S3TileService, "get_native_sources", fail)
    body = (
        {"coordinates": [[0.1, 0.2]]}
        if endpoint == "elevations"
        else horizon({"type": "Point", "coordinates": [0.1, 0.2]})
    )
    response = await client.post(f"/api/v1/{endpoint}", json=body, headers=AUTH)
    assert response.status_code == expected
    assert response.json() == {"detail": "Safe upstream error"}


async def test_horizon_deadline_covers_acquisition(client, create_test_user, terrain, monkeypatch):
    await create_test_user()
    monkeypatch.setattr(settings, "area_horizon_timeout_seconds", 0.02)

    async def slow(self, tile_ids):
        await asyncio.Event().wait()

    monkeypatch.setattr(S3TileService, "get_native_sources", slow)
    response = await client.post(
        "/api/v1/area-horizon",
        headers=AUTH,
        json=horizon({"type": "Point", "coordinates": [0.1, 0.2]}),
    )
    assert response.status_code == 504
    assert response.json()["code"] == "area_horizon_timeout"


async def test_raster_work_keeps_event_loop_responsive(
    client, create_test_user, terrain, tmp_path, monkeypatch
):
    import threading

    from app.services import native_dem

    await create_test_user()
    raster(terrain, tmp_path, [[1, 2], [3, 4]])
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = native_dem.cell_index

    def paused(source, coordinate):
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(timeout=2):
            raise AssertionError("Event loop could not release raster worker")
        return original(source, coordinate)

    monkeypatch.setattr(native_dem, "cell_index", paused)
    task = asyncio.create_task(
        client.post(
            "/api/v1/area-horizon",
            headers=AUTH,
            json=horizon({"type": "Point", "coordinates": [0.1, 0.2]}),
        )
    )
    try:
        async with asyncio.timeout(1):
            await entered.wait()
            health = await client.get("/api/v1/health", headers=AUTH)
            assert health.status_code == 200
    finally:
        release.set()
    assert (await task).status_code == 200


async def test_horizon_worker_cooperatively_stops_on_deadline(
    client, create_test_user, terrain, tmp_path, monkeypatch
):
    import threading
    import time

    from app.services import area_horizon

    await create_test_user()
    raster(terrain, tmp_path, [[1, 2], [3, 4]])
    entered, stopped = threading.Event(), threading.Event()
    original = area_horizon.process_batch

    def delayed(*args, **kwargs):
        entered.set()
        time.sleep(0.03)
        return original(*args, **kwargs)

    real_process = area_horizon.process_area

    def monitored(*args, **kwargs):
        try:
            return real_process(*args, **kwargs)
        finally:
            stopped.set()

    monkeypatch.setattr(area_horizon, "process_batch", delayed)
    monkeypatch.setattr(area_horizon, "process_area", monitored)
    monkeypatch.setattr(settings, "area_horizon_timeout_seconds", 0.02)
    response = await client.post(
        "/api/v1/area-horizon", headers=AUTH, json=horizon(mapping(box(0.1, 0.1, 0.2, 0.2)))
    )
    assert response.status_code == 504
    assert entered.is_set()
    assert await asyncio.to_thread(stopped.wait, 1)


async def test_antipodal_polygon_does_not_create_false_nearby_witness(
    client,
    create_test_user,
    terrain,
    tmp_path,
):
    await create_test_user()
    raster(
        terrain,
        tmp_path,
        [[0, 0], [0, 0]],
        tile_id="Copernicus_DSM_10_N00_00_E179_00",
        west=179.9998,
        north=0.0002,
        step=0.0001,
    )
    raster(
        terrain,
        tmp_path,
        [[0, 0], [0, 0]],
        tile_id="Copernicus_DSM_10_S01_00_E179_00",
        west=179.9998,
        north=0,
        step=0.0001,
    )
    geometry = mapping(box(179.9998, -0.0001, 180, 0.0001))
    response = await client.post(
        "/api/v1/area-horizon", headers=AUTH, json=horizon(geometry, observer_coordinates=[0, 0])
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "beyond_horizon"
    assert body["evidence"]["distance_km"] > 20000
    from shapely.geometry import Point, shape

    assert shape(geometry).covers(Point(body["evidence"]["coordinates"]))


def test_candidate_planning_observes_deadline():
    from app.exceptions import AreaHorizonTimeoutError
    from app.services.native_dem import candidate_windows

    with pytest.raises(AreaHorizonTimeoutError):
        candidate_windows(box(0, 0, 1, 1), from_origin(0, 1, 0.5, 0.5), 2, 2, deadline=0)


async def test_uncovered_part_of_real_raster_is_unavailable(
    client, create_test_user, terrain, tmp_path
):
    await create_test_user()
    # A real source file does not prove coverage outside its actual raster bounds.
    raster(terrain, tmp_path, [[0]], north=0.2, step=0.1)
    response = await client.post(
        "/api/v1/area-horizon", headers=AUTH, json=horizon(mapping(box(0.05, 0.15, 0.15, 0.16)))
    )
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "indeterminate"
    assert response.json()["unavailable_cells"] > 0


async def test_true_evidence_tie_uses_longitude(client, create_test_user, terrain, tmp_path):
    await create_test_user()
    raster(terrain, tmp_path, [[0, 0], [0, 0]])
    raster(terrain, tmp_path, [[0, 0], [0, 0]], tile_id="Copernicus_DSM_10_N00_00_W001_00", west=-1)
    geometry = {"type": "MultiPoint", "coordinates": [[0.25, 0], [-0.25, 0]]}
    response = await client.post(
        "/api/v1/area-horizon", headers=AUTH, json=horizon(geometry, observer_coordinates=[0, 0])
    )
    assert response.status_code == 200, response.text
    assert response.json()["evidence"]["coordinates"] == [-0.25, 0]


async def test_wholly_missing_polygon_is_indeterminate(client, create_test_user, terrain):
    await create_test_user()
    response = await client.post(
        "/api/v1/area-horizon", headers=AUTH, json=horizon(mapping(box(0.1, 0.1, 0.101, 0.101)))
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "indeterminate"
    assert body["evaluated_cells"] == 0
    assert body["unavailable_cells"] > 0
    assert body["evidence"] is None


async def test_polygon_reads_bounded_windows(
    client, create_test_user, terrain, tmp_path, monkeypatch
):
    from app.services import area_horizon

    await create_test_user()
    raster(terrain, tmp_path, [[0] * 128 for _ in range(64)], north=0.5, step=1 / 128)
    original = area_horizon.process_batch

    def bounded(plan, index, *args, **kwargs):
        window = plan.windows[index]
        assert window.width * window.height <= 4096
        return original(plan, index, *args, **kwargs)

    monkeypatch.setattr(area_horizon, "process_batch", bounded)
    response = await client.post(
        "/api/v1/area-horizon", headers=AUTH, json=horizon(mapping(box(0.01, 0.01, 0.99, 0.49)))
    )
    assert response.status_code == 200, response.text
    assert response.json()["evaluated_cells"] > 4096
    assert response.json()["coverage_complete"] is True


async def test_native_nodata_nan_and_negative_horizon_height(
    client, create_test_user, terrain, tmp_path
):
    await create_test_user()
    raster(terrain, tmp_path, [[float("nan"), -5], [0, -9999]])
    elevation = await client.post(
        "/api/v1/elevations", headers=AUTH, json={"coordinates": [[0.2, 0.8], [0.8, 0.8]]}
    )
    assert [r["elevation_m"] for r in elevation.json()["results"]] == [None, -5]
    response = await client.post(
        "/api/v1/area-horizon",
        headers=AUTH,
        json=horizon({"type": "Point", "coordinates": [0.8, 0.8]}, target_height_agl_m=2),
    )
    assert response.status_code == 200, response.text
    assert response.json()["evidence"]["horizon_km"] == 0
    assert response.json()["evidence"]["elevation_m"] == -5


async def test_tied_longitudes_use_latitude(client, create_test_user, terrain, tmp_path):
    await create_test_user()
    raster(terrain, tmp_path, [[0, 0], [0, 0]])
    raster(terrain, tmp_path, [[0, 0], [0, 0]], tile_id="Copernicus_DSM_10_S01_00_E000_00", north=0)
    response = await client.post(
        "/api/v1/area-horizon",
        headers=AUTH,
        json=horizon(
            {"type": "MultiPoint", "coordinates": [[0, 0.25], [0, -0.25]]},
            observer_coordinates=[0, 0],
        ),
    )
    assert response.status_code == 200, response.text
    assert response.json()["evidence"]["coordinates"] == [0, -0.25]


@pytest.mark.parametrize("endpoint", ["elevations", "area-horizon"])
async def test_swagger_request_examples_succeed_unchanged(
    client,
    create_test_user,
    terrain,
    tmp_path,
    endpoint,
):
    await create_test_user()
    raster(
        terrain,
        tmp_path,
        [[100, 100], [100, 100]],
        tile_id="Copernicus_DSM_10_S40_00_E174_00",
        west=174,
        north=-39,
    )
    document = await client.get("/openapi.json", headers=AUTH)
    assert document.status_code == 200
    openapi = document.json()
    request_schema = openapi["paths"][f"/api/v1/{endpoint}"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    schema_name = request_schema["$ref"].rsplit("/", 1)[-1]
    schema = openapi["components"]["schemas"][schema_name]
    example = schema.get("example")
    assert isinstance(example, dict), "Swagger must supply a complete request example"
    assert set(example) == set(schema["properties"])

    response = await client.post(f"/api/v1/{endpoint}", json=example, headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    if endpoint == "elevations":
        assert body["results"]
        assert all(result["status"] == "ok" for result in body["results"])
    else:
        assert body["outcome"] == "within_horizon"
        assert body["coverage_complete"] is False
        assert body["reason"] == "stopped_at_witness"
