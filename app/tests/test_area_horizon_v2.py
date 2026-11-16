"""Candidate sampling and pruning must preserve the v2 decision contract."""

import math
from threading import Event

import numpy as np
import pytest
from pyproj import Geod
from shapely.geometry import box, mapping

from app.schemas.terrain import AreaHorizonRequest
from app.services import area_horizon
from app.services.s3_tiles import NativeTile, S3TileService
from app.tests.test_terrain_api import AUTH, TILE, horizon, raster
from app.tests.test_terrain_api import terrain as terrain_fixture

terrain = terrain_fixture


def run_polygon(tmp_path, values, geometry=None, *, prune=True, **overrides):
    sources = {}
    path = raster(sources, tmp_path, values, step=1 / len(values))
    request = AreaHorizonRequest.model_validate(
        horizon(
            mapping(geometry if geometry is not None else box(0.001, 0.001, 0.999, 0.999)),
            observer_coordinates=[-1, 0.5],
            **overrides,
        )
    )
    stats = area_horizon.ProcessingStats()
    _, pieces = area_horizon.prepare_area(request, 25_000_000, math.inf, Event())
    result = area_horizon.process_area(
        request,
        None,
        pieces,
        {TILE: NativeTile(TILE, "test/product", path)},
        25_000_000,
        math.inf,
        Event(),
        stats=stats,
        prune=prune,
    )
    return result, stats


def test_flat_area_prunes_most_batches(tmp_path):
    result, stats = run_polygon(tmp_path, [[0] * 512 for _ in range(512)])
    assert result.outcome == "beyond_horizon"
    assert result.coverage_complete
    assert stats.pruned_batches > stats.distance_batches
    assert result.evaluated_cells > 60_000
    assert result.provenance.processor_version == "area-horizon-v2"
    assert result.provenance.sampling == "native_cell_centre"
    assert result.provenance.tolerance_km == 0.05


def test_high_cell_in_later_batch_is_not_pruned(tmp_path):
    values = np.zeros((256, 256))
    values[210, 210] = 10000
    result, _ = run_polygon(tmp_path, values.tolist())
    assert result.outcome == "within_horizon"
    assert result.evidence.elevation_m == 10000
    assert not result.coverage_complete
    assert result.reason == "stopped_at_witness"


def test_pruning_matches_exhaustive_candidates(tmp_path):
    values = np.random.default_rng(2026).uniform(-10, 1, (512, 512)).tolist()
    pruned, stats = run_polygon(tmp_path, values)
    exhaustive, _ = run_polygon(tmp_path, values, prune=False)
    assert pruned.outcome == exhaustive.outcome == "beyond_horizon"
    assert pruned.evidence == exhaustive.evidence
    assert pruned.evaluated_cells == exhaustive.evaluated_cells
    assert pruned.unavailable_cells == exhaustive.unavailable_cells
    assert stats.pruned_batches > 0


async def test_v2_observer_inside_stops_at_one_cell(client, create_test_user, terrain, tmp_path):
    await create_test_user()
    raster(terrain, tmp_path, [[100, 100], [100, 100]])
    response = await client.post(
        "/api/v1/area-horizon",
        headers=AUTH,
        json=horizon(mapping(box(0.1, 0.1, 0.9, 0.9)), observer_coordinates=[0.25, 0.25]),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "within_horizon"
    assert body["evaluated_cells"] == 1
    assert body["reason"] == "stopped_at_witness"
    assert body["coverage_complete"] is False
    assert body["evidence"]["distance_km"] == 0


@pytest.mark.parametrize(
    "offset_m,expected",
    [(49.9, "within_horizon"), (50.1, "beyond_horizon")],
)
async def test_polygon_tolerance(client, create_test_user, terrain, tmp_path, offset_m, expected):
    await create_test_user()
    # A sliver on the equator has no cell centre. Its nearest endpoint is controlled in metres.
    x, _, _ = Geod(ellps="WGS84").fwd(0, 0, 90, offset_m)
    raster(terrain, tmp_path, [[0, 0], [0, 0]])
    response = await client.post(
        "/api/v1/area-horizon",
        headers=AUTH,
        json=horizon(mapping(box(x, 0, x + 0.000001, 0.000001)), observer_coordinates=[0, 0]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == expected
    assert response.json()["evaluated_cells"] == 1


@pytest.mark.parametrize("reason", ["catalogue_product_not_found", "dem_object_not_found"])
async def test_unpublished_area_is_sea_but_elevations_stay_missing(
    client,
    create_test_user,
    monkeypatch,
    reason,
):
    from app.services.s3_tiles import UnavailableTile

    await create_test_user()

    async def acquire(self, tile_ids):
        return {tile_id: UnavailableTile(tile_id, reason) for tile_id in tile_ids}

    monkeypatch.setattr(S3TileService, "get_native_sources", acquire)
    response = await client.post(
        "/api/v1/area-horizon",
        headers=AUTH,
        json=horizon({"type": "Point", "coordinates": [0.1, 0.2]}),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "beyond_horizon"
    assert body["coverage_complete"] is True
    assert body["unavailable_cells"] == 0
    assert body["evidence"]["elevation_m"] == 0
    assert body["provenance"]["sea_level_assumed_tiles"] == [TILE]
    assert body["provenance"]["tiles"] == []
    elevation = await client.post(
        "/api/v1/elevations", headers=AUTH, json={"coordinates": [[0.1, 0.2]]}
    )
    assert elevation.json()["results"][0]["status"] == "unavailable"
    assert "sea_level_assumed_tiles" not in elevation.json()["provenance"]


async def test_restricted_area_is_not_assumed_sea(client, create_test_user, monkeypatch):
    from app.services.s3_tiles import UnavailableTile

    await create_test_user()

    async def acquire(self, tile_ids):
        return {tile_id: UnavailableTile(tile_id, "tile_restricted") for tile_id in tile_ids}

    monkeypatch.setattr(S3TileService, "get_native_sources", acquire)
    response = await client.post(
        "/api/v1/area-horizon",
        headers=AUTH,
        json=horizon({"type": "Point", "coordinates": [0.1, 0.2]}),
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "indeterminate"
    assert response.json()["provenance"]["sea_level_assumed_tiles"] == []


async def test_point_keeps_exact_threshold(client, create_test_user, terrain, tmp_path):
    await create_test_user()
    raster(terrain, tmp_path, [[0, 0], [0, 0]])
    x, _, _ = Geod(ellps="WGS84").fwd(0, 0, 90, 25)
    response = await client.post(
        "/api/v1/area-horizon",
        headers=AUTH,
        json=horizon({"type": "Point", "coordinates": [x, 0]}, observer_coordinates=[0, 0]),
    )
    assert response.json()["outcome"] == "beyond_horizon"
    assert response.json()["coverage_complete"] is True
    assert response.json()["provenance"]["tolerance_km"] == 0


def test_interior_tile_has_no_original_boundary_candidates():
    from rasterio.transform import from_origin

    from app.services.horizon_sampling import RasterPlan, boundary_candidates
    from app.services.s3_tiles import UnavailableTile

    target = box(-0.1, -0.1, 1.1, 1.1)
    plan = RasterPlan(
        UnavailableTile(TILE, "tile_restricted"),
        from_origin(0, 1, 0.5, 0.5),
        2,
        2,
        box(0, 0, 1, 1),
        target,
    )
    assert boundary_candidates(plan, target.boundary, math.inf, Event()) == {}


async def test_unpublished_polygon_is_evaluated_without_a_synthetic_file(
    client,
    create_test_user,
    monkeypatch,
):
    from app.services.s3_tiles import UnavailableTile

    await create_test_user()

    async def acquire(self, tile_ids):
        return {
            tile_id: UnavailableTile(tile_id, "catalogue_product_not_found") for tile_id in tile_ids
        }

    monkeypatch.setattr(S3TileService, "get_native_sources", acquire)
    response = await client.post(
        "/api/v1/area-horizon", headers=AUTH, json=horizon(mapping(box(0.1, 0.1, 0.101, 0.101)))
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "beyond_horizon"
    assert body["coverage_complete"] is True
    assert body["evaluated_cells"] > 0
    assert body["unavailable_cells"] == 0
    assert body["provenance"]["sea_level_assumed_tiles"] == [TILE]
    assert body["provenance"]["tiles"] == []


def test_tolerance_comparison_includes_equality_without_rounding():
    from app.schemas.terrain import HorizonEvidence

    request = AreaHorizonRequest.model_validate(horizon(mapping(box(0.1, 0.1, 0.2, 0.2))))
    accumulator = area_horizon.HorizonAccumulator(request, polygon=True, evaluated=1)
    accumulator.evidence = HorizonEvidence(
        coordinates=[0.1, 0.1], elevation_m=0, distance_km=0.05, horizon_km=0
    )
    assert accumulator.response().outcome == "within_horizon"
    accumulator.evidence.distance_km = math.nextafter(0.05, math.inf)
    assert accumulator.response().outcome == "beyond_horizon"
