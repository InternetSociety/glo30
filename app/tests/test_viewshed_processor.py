import math
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import shape

from app.config import Settings
from app.schemas.viewshed import ViewshedRequest
from app.services.viewshed import ViewshedProcessor, vertex_count


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
    )
    request = ViewshedRequest(
        observer_coordinates=(174.5, -39.5),
        observer_height_agl_m=30,
        target_height_agl_m=0,
        radius_m=300,
    )

    result = ViewshedProcessor(settings).process([tile_path], request)

    geometry = shape(result.geometry)
    vertex_budget = max(4, math.ceil(result.visible_area_sq_km * 10))
    assert result.visible_pixel_count > 250
    assert result.visible_area_sq_km > 0.2
    assert geometry.geom_type in {"Polygon", "MultiPolygon"}
    assert vertex_count(geometry) <= vertex_budget
    assert geometry.bounds[0] >= 174.49
    assert geometry.bounds[2] <= 174.51
