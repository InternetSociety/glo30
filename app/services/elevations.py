import asyncio

from app.config import Settings
from app.exceptions import TerrainLimitError
from app.schemas.terrain import (
    ElevationRequest,
    ElevationResponse,
    ElevationResult,
    TerrainProvenance,
)
from app.services.native_dem import point_tile, sample_points
from app.services.s3_tiles import S3TileService


class ElevationService:
    def __init__(self, tile_service: S3TileService, settings: Settings) -> None:
        self.tile_service = tile_service
        self.settings = settings

    async def create(self, request: ElevationRequest) -> ElevationResponse:
        if len(request.coordinates) > self.settings.elevation_max_batch_points:
            raise TerrainLimitError("Elevation batch-point limit exceeded", code="elevation_limit")
        sources = await self.tile_service.get_native_tiles(
            sorted({point_tile(point) for point in request.coordinates})
        )
        values, tiles = await asyncio.to_thread(sample_points, request.coordinates, sources)
        return ElevationResponse(
            results=[
                ElevationResult(
                    coordinates=point,
                    status="ok" if value is not None else "unavailable",
                    elevation_m=value,
                    reason=None if value is not None else "dem_unavailable",
                )
                for point, value in zip(request.coordinates, values, strict=True)
            ],
            provenance=TerrainProvenance(
                sampling="native_cell", processor_version="elevation-v1", tiles=tiles
            ),
        )
