from pathlib import Path

import numpy as np
import pytest
import rasterio
from pydantic import ValidationError
from rasterio.transform import from_origin
from shapely.geometry import shape

from app.config import Settings
from app.schemas.viewshed import ViewshedRequest
from app.services.viewshed import (
    ViewshedProcessor,
    vertex_budget_for_visible_area,
    vertex_count,
)


def write_flat_dem(path: Path) -> None:
    data = np.full((240, 240), 100, dtype=np.float32)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=data.shape[1],
        height=data.shape[0],
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(174.47, -39.47, 0.00025, 0.00025),
        nodata=-9999,
    ) as destination:
        destination.write(data, 1)


def test_processor_runs_full_gdal_pipeline_on_flat_dem(tmp_path: Path) -> None:
    tile_path = tmp_path / "flat-dem.tif"
    write_flat_dem(tile_path)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        tile_cache_dir=tmp_path,
        dem_resolution_m=30,
        geometry_max_vertex_budget=12,
    )
    request = ViewshedRequest(
        observer_coordinates=(174.5, -39.5),
        observer_height_agl_m=30,
        target_height_agl_m=0,
        radius_m=300,
    )

    result = ViewshedProcessor(settings).process([tile_path], request)

    geometry = shape(result.geometry)
    vertex_budget = vertex_budget_for_visible_area(
        result.visible_area_sq_km,
        vertices_per_sq_km=settings.geometry_vertices_per_sq_km,
        minimum_vertex_budget=settings.geometry_min_vertex_budget,
        maximum_vertex_budget=settings.geometry_max_vertex_budget,
    )
    assert result.visible_pixel_count > 250
    assert result.visible_area_sq_km > 0.2
    assert vertex_budget == settings.geometry_max_vertex_budget
    assert geometry.geom_type in {"Polygon", "MultiPolygon"}
    assert vertex_count(geometry) <= vertex_budget
    assert geometry.bounds[0] >= 174.49
    assert geometry.bounds[2] <= 174.51


def test_vertex_budget_scales_with_area_between_configured_limits() -> None:
    settings = Settings(_env_file=None)

    def budget(visible_area_sq_km: float) -> int:
        return vertex_budget_for_visible_area(
            visible_area_sq_km,
            vertices_per_sq_km=settings.geometry_vertices_per_sq_km,
            minimum_vertex_budget=settings.geometry_min_vertex_budget,
            maximum_vertex_budget=settings.geometry_max_vertex_budget,
        )

    assert [budget(area) for area in (0, 0.08, 0.0801, 25, 100, 100.01)] == [
        8,
        8,
        9,
        2500,
        10_000,
        10_000,
    ]


def test_vertex_budget_accepts_custom_resolution_settings() -> None:
    assert (
        vertex_budget_for_visible_area(
            10.0782,
            vertices_per_sq_km=100,
            minimum_vertex_budget=16,
            maximum_vertex_budget=2000,
        )
        == 1008
    )


def test_settings_reject_maximum_vertex_budget_below_minimum() -> None:
    with pytest.raises(ValidationError, match="geometry_max_vertex_budget"):
        Settings(
            _env_file=None,
            geometry_min_vertex_budget=16,
            geometry_max_vertex_budget=15,
        )
