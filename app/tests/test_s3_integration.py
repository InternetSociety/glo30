import os
from datetime import datetime
from pathlib import Path

import pytest
from shapely.geometry import shape

from app.config import settings
from app.models.cached_tile import CachedTile
from app.schemas.viewshed import ViewshedRequest
from app.services.s3_tiles import S3TileService
from app.services.viewshed import ViewshedProcessor, vertex_budget_for_visible_area, vertex_count


class IntegrationTileRepository:
    def __init__(self) -> None:
        self.tiles: dict[str, CachedTile] = {}

    async def get_by_tile_id(self, tile_id: str) -> CachedTile | None:
        return self.tiles.get(tile_id)

    async def list_expired(self, now: datetime) -> list[CachedTile]:
        return []

    async def add(self, tile: CachedTile) -> CachedTile:
        self.tiles[tile.tile_id] = tile
        return tile

    async def delete(self, tile: CachedTile) -> None:
        self.tiles.pop(tile.tile_id, None)


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_S3_INTEGRATION") != "1",
    reason="set RUN_S3_INTEGRATION=1 to use Copernicus S3",
)
@pytest.mark.asyncio
async def test_real_s3_tile_and_viewshed_pipeline(tmp_path: Path) -> None:
    integration_settings = settings.model_copy(
        update={"data_dir": tmp_path, "tile_cache_dir": tmp_path}
    )
    request = ViewshedRequest(
        observer_coordinates=(174.2316077, -39.0035668),
        observer_height_agl_m=30,
        target_height_agl_m=0,
        radius_m=300,
    )
    tile_service = S3TileService(IntegrationTileRepository(), integration_settings)

    tiles = await tile_service.get_tiles(
        request.longitude,
        request.latitude,
        request.radius_m,
    )
    result = ViewshedProcessor(integration_settings).process(tiles, request)

    geometry = shape(result.geometry)
    budget = vertex_budget_for_visible_area(
        result.visible_area_sq_km,
        vertices_per_sq_km=integration_settings.geometry_vertices_per_sq_km,
        minimum_vertex_budget=integration_settings.geometry_min_vertex_budget,
        maximum_vertex_budget=integration_settings.geometry_max_vertex_budget,
    )
    assert result.visible_pixel_count > 0
    assert not geometry.is_empty
    assert vertex_count(geometry) <= budget
