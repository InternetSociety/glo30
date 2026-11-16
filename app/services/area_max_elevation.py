"""One-pass native terrain maxima, with independent area and deadline limits."""

import asyncio
import logging
from dataclasses import dataclass, field
from threading import Event
from time import monotonic
from typing import Any

import numpy as np
import rasterio
from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry

from app.config import Settings
from app.exceptions import AreaHorizonTimeoutError, AreaMaxElevationTimeoutError, TerrainLimitError
from app.schemas.terrain import (
    AreaMaxElevationProvenance,
    AreaMaxElevationRequest,
    AreaMaxElevationResponse,
    TileProvenance,
)
from app.services.horizon_sampling import RasterPlan, cell_centres, centre_mask, plan_batches
from app.services.native_dem import (
    candidate_windows,
    check_deadline,
    geometry_tiles,
    nominal_grid,
    open_native,
)
from app.services.s3_tiles import MISSING_TILE_REASONS, NativeTile, S3TileService, UnavailableTile

logger = logging.getLogger("uvicorn.error")


def check_limit(count: int, maximum: int) -> None:
    if count > maximum:
        raise TerrainLimitError(
            "Area maximum elevation candidate-cell limit exceeded", code="area_max_elevation_limit"
        )


def window_count(
    geometry: BaseGeometry,
    transform: Any,
    width: int,
    height: int,
    deadline: float,
    cancelled: Event,
) -> int:
    return sum(
        int(w.width * w.height)
        for w in candidate_windows(
            geometry,
            transform,
            width,
            height,
            deadline=deadline,
            cancelled=cancelled,
        )
    )


def prepare(
    request: AreaMaxElevationRequest, maximum: int, deadline: float, cancelled: Event
) -> dict[str, BaseGeometry]:
    pieces = geometry_tiles(shape(request.geometry.model_dump()), deadline, cancelled)
    count = 0
    for tile_id, piece in pieces.items():
        check_deadline(deadline, cancelled)
        transform, width, height = nominal_grid(tile_id)
        count += window_count(piece, transform, width, height, deadline, cancelled)
        check_limit(count, maximum)
    return pieces


@dataclass
class Maximum:
    elevation: float | None = None
    coordinates: tuple[float, float] | None = None
    evaluated: int = 0
    unavailable: int = 0
    tiles: list[TileProvenance] = field(default_factory=list)
    sea_tiles: set[str] = field(default_factory=set)

    def consume(self, plan: RasterPlan, index: int, data: Any) -> None:
        window = plan.windows[index]
        selected = centre_mask(plan, window, bool(plan.inside[index])).ravel()
        boundary = plan.boundary.get(plan.block_key(window))
        if boundary is not None:
            offsets = (
                (boundary[:, 2].astype(np.int64) - int(window.row_off)) * int(window.width)
                + boundary[:, 3].astype(np.int64)
                - int(window.col_off)
            )
            selected[offsets] = True
        values = np.asarray(data.data, dtype=np.float64).ravel()
        valid = selected & ~np.ma.getmaskarray(data).ravel() & np.isfinite(values)
        self.evaluated += int(np.count_nonzero(valid))
        self.unavailable += int(np.count_nonzero(selected & ~valid))
        if not np.any(valid):
            return
        maximum = float(np.max(values[valid]))
        if self.elevation is not None and maximum < self.elevation:
            return
        xs, ys = cell_centres(plan, window)
        ties = valid & (values == maximum)
        longitudes, latitudes = xs.ravel()[ties], ys.ravel()[ties]
        best = np.lexsort((latitudes, longitudes))[0]
        coordinates = (float(longitudes[best]), float(latitudes[best]))
        if (
            self.elevation is None
            or maximum > self.elevation
            or (self.coordinates is not None and coordinates < self.coordinates)
        ):
            self.elevation, self.coordinates = maximum, coordinates

    def response(self) -> AreaMaxElevationResponse:
        return AreaMaxElevationResponse(
            status="ok" if self.elevation is not None else "unavailable",
            max_elevation_m=self.elevation,
            coordinates=self.coordinates,
            evaluated_cells=self.evaluated,
            unavailable_cells=self.unavailable,
            coverage_complete=self.unavailable == 0,
            provenance=AreaMaxElevationProvenance(
                tiles=sorted(self.tiles, key=lambda tile: tile.tile_id),
                sea_level_assumed_tiles=sorted(self.sea_tiles),
            ),
        )


def process(
    request: AreaMaxElevationRequest,
    pieces: dict[str, BaseGeometry],
    sources: dict[str, NativeTile | UnavailableTile],
    maximum: int,
    deadline: float,
    cancelled: Event,
) -> AreaMaxElevationResponse:
    target = shape(request.geometry.model_dump())
    plans = []
    count = 0
    # Check the actual grids before the single data-reading pass.
    for tile_id, piece in pieces.items():
        check_deadline(deadline, cancelled)
        source = sources[tile_id]
        if isinstance(source, NativeTile):
            with rasterio.open(source.path) as dataset:
                transform, width, height = dataset.transform, dataset.width, dataset.height
                if (
                    dataset.crs != rasterio.crs.CRS.from_epsg(4326)
                    or transform.b
                    or transform.d
                    or transform.a <= 0
                    or transform.e >= 0
                ):
                    raise ValueError("Native DEM must be a north-up EPSG:4326 raster")
                extent = box(*dataset.bounds)
            uncovered = piece.difference(extent)
            if not uncovered.is_empty:
                missing_transform, missing_width, missing_height = nominal_grid(tile_id)
                plans.append(
                    RasterPlan(
                        UnavailableTile(tile_id, "outside_source"),
                        missing_transform,
                        missing_width,
                        missing_height,
                        uncovered,
                        uncovered,
                    )
                )
            plans.append(
                RasterPlan(source, transform, width, height, piece.intersection(extent), target)
            )
        else:
            transform, width, height = nominal_grid(tile_id)
            plans.append(RasterPlan(source, transform, width, height, piece, target))
    for plan in plans:
        if plan.piece.is_empty:
            continue
        count += window_count(
            plan.piece, plan.transform, plan.width, plan.height, deadline, cancelled
        )
        check_limit(count, maximum)
    result = Maximum()
    for plan in plans:
        if plan.piece.is_empty:
            continue
        plan_batches(plan, target.boundary, None, deadline, cancelled)
        if isinstance(plan.source, NativeTile):
            with open_native(plan.source, deadline, cancelled) as (dataset, metadata):
                result.tiles.append(metadata)
                for index, window in enumerate(plan.windows):
                    check_deadline(deadline, cancelled)
                    if plan.outside[index] and plan.block_key(window) not in plan.boundary:
                        continue
                    result.consume(plan, index, dataset.read(1, window=window, masked=True))
        else:
            sea = plan.source.reason in MISSING_TILE_REASONS
            before = result.evaluated
            for index, window in enumerate(plan.windows):
                check_deadline(deadline, cancelled)
                if plan.outside[index] and plan.block_key(window) not in plan.boundary:
                    continue
                result.consume(
                    plan,
                    index,
                    np.ma.array(np.zeros((int(window.height), int(window.width))), mask=not sea),
                )
            if sea and result.evaluated > before:
                result.sea_tiles.add(plan.source.tile_id)
    check_deadline(deadline, cancelled)
    return result.response()


class AreaMaxElevationService:
    def __init__(self, tile_service: S3TileService, settings: Settings) -> None:
        self.tile_service, self.settings = tile_service, settings

    async def create(self, request: AreaMaxElevationRequest) -> AreaMaxElevationResponse:
        cancelled = Event()
        duration = self.settings.area_max_elevation_timeout_seconds
        deadline = monotonic() + duration
        started = monotonic()
        try:
            async with asyncio.timeout(duration):
                pieces = await asyncio.to_thread(
                    prepare,
                    request,
                    self.settings.area_max_elevation_max_cells,
                    deadline,
                    cancelled,
                )
                sources = await self.tile_service.get_native_sources(list(pieces))
                return await asyncio.to_thread(
                    process,
                    request,
                    pieces,
                    sources,
                    self.settings.area_max_elevation_max_cells,
                    deadline,
                    cancelled,
                )
        except (TimeoutError, AreaHorizonTimeoutError) as exc:
            raise AreaMaxElevationTimeoutError(
                "Area maximum elevation processing timed out", code="area_max_elevation_timeout"
            ) from exc
        finally:
            cancelled.set()
            logger.info("Area maximum elevation processed: elapsed=%.3fs", monotonic() - started)
