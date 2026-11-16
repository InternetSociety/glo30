"""Smooth-earth radio horizon with bounded native-centre sampling and batch pruning."""

import asyncio
import logging
import math
from dataclasses import dataclass, field
from threading import Event
from time import monotonic
from typing import Any, Literal

import numpy as np
import rasterio
from pyproj import Geod
from shapely.geometry import Point, box, shape
from shapely.geometry.base import BaseGeometry

from app.config import Settings
from app.exceptions import AreaHorizonTimeoutError
from app.schemas.terrain import (
    AreaHorizonProvenance,
    AreaHorizonRequest,
    AreaHorizonResponse,
    HorizonEvidence,
    TileProvenance,
)
from app.services.horizon_sampling import RasterPlan, cell_centres, centre_mask, plan_batches
from app.services.native_dem import (
    candidate_windows,
    check_deadline,
    enforce_cell_limit,
    geometry_tiles,
    nominal_grid,
    open_native,
    point_tile,
    sample_points,
)
from app.services.s3_tiles import MISSING_TILE_REASONS, NativeTile, S3TileService, UnavailableTile

logger = logging.getLogger("uvicorn.error")
POLYGON_TOLERANCE_KM = 0.05


@dataclass
class ProcessingStats:
    read_batches: int = 0
    pruned_batches: int = 0
    distance_batches: int = 0


def assumed_sea(source: NativeTile | UnavailableTile) -> bool:
    return isinstance(source, UnavailableTile) and source.reason in MISSING_TILE_REASONS


def horizon_km(request: AreaHorizonRequest, elevations: Any) -> Any:
    coefficient = 4.12 * math.sqrt(request.effective_earth_radius_factor / (4 / 3))
    return coefficient * (
        math.sqrt(max(0, request.observer_antenna_height_asl_m))
        + np.sqrt(np.maximum(0, elevations + request.target_height_agl_m))
    )


@dataclass
class HorizonAccumulator:
    request: AreaHorizonRequest
    polygon: bool
    evaluated: int = 0
    unavailable: int = 0
    evidence: HorizonEvidence | None = None
    tiles: list[TileProvenance] = field(default_factory=list)
    sea_tiles: set[str] = field(default_factory=set)

    @property
    def margin(self) -> float:
        return self.evidence.distance_km - self.evidence.horizon_km if self.evidence else math.inf

    @property
    def witness(self) -> bool:
        return self.evidence is not None and self.margin <= (
            POLYGON_TOLERANCE_KM if self.polygon else 0
        )

    def consider(self, xs: Any, ys: Any, elevations: Any) -> None:
        if not len(elevations):
            return
        longitude, latitude = self.request.observer_coordinates
        _, _, distances = Geod(ellps="WGS84").inv(
            np.full(len(elevations), longitude),
            np.full(len(elevations), latitude),
            xs,
            ys,
        )
        distances = np.asarray(distances) / 1000
        horizons = horizon_km(self.request, elevations)
        if not np.all(np.isfinite(distances)):
            raise ValueError("Cannot calculate finite geodesic evidence distances")
        index = np.lexsort((ys, xs, distances - horizons))[0]
        evidence = HorizonEvidence(
            coordinates=(float(xs[index]), float(ys[index])),
            elevation_m=float(elevations[index]),
            distance_km=float(distances[index]),
            horizon_km=float(horizons[index]),
        )
        if self.evidence is None or (
            evidence.distance_km - evidence.horizon_km,
            *evidence.coordinates,
        ) < (
            self.margin,
            *self.evidence.coordinates,
        ):
            self.evidence = evidence

    def response(self, *, stopped: bool = False) -> AreaHorizonResponse:
        complete = self.unavailable == 0 and not stopped
        outcome: Literal["within_horizon", "beyond_horizon", "indeterminate"] = (
            "within_horizon" if self.witness else "beyond_horizon" if complete else "indeterminate"
        )
        reason: Literal["incomplete_dem", "stopped_at_witness"] | None = (
            "stopped_at_witness"
            if stopped
            else "incomplete_dem"
            if outcome == "indeterminate"
            else None
        )
        return AreaHorizonResponse(
            outcome=outcome,
            observer_coordinates=self.request.observer_coordinates,
            observer_antenna_height_asl_m=max(0, self.request.observer_antenna_height_asl_m),
            target_height_agl_m=self.request.target_height_agl_m,
            effective_earth_radius_factor=self.request.effective_earth_radius_factor,
            coverage_complete=complete,
            evaluated_cells=self.evaluated,
            unavailable_cells=self.unavailable,
            evidence=self.evidence,
            reason=reason,
            provenance=AreaHorizonProvenance(
                sampling="native_cell_centre" if self.polygon else "native_cell",
                tolerance_km=POLYGON_TOLERANCE_KM if self.polygon else 0,
                sea_level_assumed_tiles=sorted(self.sea_tiles),
                tiles=sorted(self.tiles, key=lambda tile: tile.tile_id),
            ),
        )


def prepare_area(
    request: AreaHorizonRequest, maximum: int, deadline: float, cancelled: Event
) -> tuple[list[tuple[float, float]] | None, dict[str, BaseGeometry]]:
    geometry = shape(request.geometry.model_dump())
    if geometry.geom_type in ("Point", "MultiPoint"):
        points = [geometry] if geometry.geom_type == "Point" else list(geometry.geoms)
        enforce_cell_limit(len(points), maximum)
        return [(point.x, point.y) for point in points], {}
    pieces = geometry_tiles(geometry, deadline, cancelled)
    count = 0
    for tile_id, piece in pieces.items():
        check_deadline(deadline, cancelled)
        transform, width, height = nominal_grid(tile_id)
        count += sum(
            int(w.width * w.height)
            for w in candidate_windows(
                piece,
                transform,
                width,
                height,
                deadline=deadline,
                cancelled=cancelled,
                max_cells=maximum - count,
            )
        )
        enforce_cell_limit(count, maximum)
    return None, pieces


def point_evaluations(
    request: AreaHorizonRequest,
    points: list[tuple[float, float]],
    sources: dict[str, NativeTile | UnavailableTile],
    deadline: float,
    cancelled: Event,
    *,
    polygon: bool = False,
) -> HorizonAccumulator:
    accumulator = HorizonAccumulator(request, polygon)
    real_sources = {
        tile_id: source if isinstance(source, NativeTile) else None
        for tile_id, source in sources.items()
    }
    values, accumulator.tiles = sample_points(points, real_sources, deadline, cancelled)
    for index, point in enumerate(points):
        tile_id = point_tile(point)
        if assumed_sea(sources[tile_id]):
            values[index] = 0.0
            accumulator.sea_tiles.add(tile_id)
    valid = [
        (point, value) for point, value in zip(points, values, strict=True) if value is not None
    ]
    accumulator.evaluated, accumulator.unavailable = len(valid), len(points) - len(valid)
    accumulator.consider(
        np.array([point[0] for point, _ in valid]),
        np.array([point[1] for point, _ in valid]),
        np.array([value for _, value in valid]),
    )
    return accumulator


def raster_plans(
    request: AreaHorizonRequest,
    pieces: dict[str, BaseGeometry],
    sources: dict[str, NativeTile | UnavailableTile],
    maximum: int,
    deadline: float,
    cancelled: Event,
) -> list[RasterPlan]:
    target = shape(request.geometry.model_dump())
    plans = []
    count = 0
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
        count += sum(
            int(w.width * w.height)
            for w in candidate_windows(
                plan.piece,
                plan.transform,
                plan.width,
                plan.height,
                deadline=deadline,
                cancelled=cancelled,
                max_cells=maximum - count,
            )
        )
        enforce_cell_limit(count, maximum)
        plan_batches(plan, target.boundary, request.observer_coordinates, deadline, cancelled)
    return sorted(
        plans, key=lambda plan: float(np.min(plan.lower_bounds)) if len(plan.windows) else math.inf
    )


def process_batch(
    plan: RasterPlan,
    index: int,
    data: Any,
    accumulator: HorizonAccumulator,
    stats: ProcessingStats,
    *,
    prune: bool,
) -> None:
    window = plan.windows[index]
    centres = centre_mask(plan, window, bool(plan.inside[index]))
    selected = centres.copy().ravel()
    boundary = plan.boundary.get(plan.block_key(window))
    offsets = np.empty(0, dtype=np.int64)
    if boundary is not None:
        offsets = (
            (boundary[:, 2].astype(np.int64) - int(window.row_off)) * int(window.width)
            + boundary[:, 3].astype(np.int64)
            - int(window.col_off)
        )
        selected[offsets] = True
    values = np.asarray(data.data, dtype=np.float64).ravel()
    valid = ~np.ma.getmaskarray(data).ravel() & np.isfinite(values)
    accumulator.evaluated += int(np.count_nonzero(selected & valid))
    accumulator.unavailable += int(np.count_nonzero(selected & ~valid))
    if not np.any(selected & valid):
        return
    if assumed_sea(plan.source):
        accumulator.sea_tiles.add(plan.source.tile_id)
    best_possible = float(plan.lower_bounds[index]) - float(
        horizon_km(accumulator.request, np.max(values[valid]))
    )
    if prune and best_possible > accumulator.margin:
        stats.pruned_batches += 1
        return
    stats.distance_batches += 1
    xs, ys = cell_centres(plan, window)
    centre_valid = centres.ravel() & valid
    xparts, yparts, heights = (
        [xs.ravel()[centre_valid]],
        [ys.ravel()[centre_valid]],
        [values[centre_valid]],
    )
    if boundary is not None:
        available = valid[offsets]
        xparts.append(boundary[available, 0])
        yparts.append(boundary[available, 1])
        heights.append(values[offsets[available]])
    accumulator.consider(np.concatenate(xparts), np.concatenate(yparts), np.concatenate(heights))


def process_area(
    request: AreaHorizonRequest,
    points: list[tuple[float, float]] | None,
    pieces: dict[str, BaseGeometry],
    sources: dict[str, NativeTile | UnavailableTile],
    maximum: int,
    deadline: float,
    cancelled: Event,
    *,
    stats: ProcessingStats | None = None,
    prune: bool = True,
) -> AreaHorizonResponse:
    if points is not None:
        return point_evaluations(request, points, sources, deadline, cancelled).response()
    stats = stats if stats is not None else ProcessingStats()
    accumulator = HorizonAccumulator(request, True)
    for plan in raster_plans(request, pieces, sources, maximum, deadline, cancelled):
        if not plan.windows:
            continue
        if isinstance(plan.source, NativeTile):
            with open_native(plan.source, deadline, cancelled) as (dataset, metadata):
                accumulator.tiles.append(metadata)
                for index in np.argsort(plan.lower_bounds, kind="stable"):
                    check_deadline(deadline, cancelled)
                    if (
                        plan.outside[index]
                        and plan.block_key(plan.windows[index]) not in plan.boundary
                    ):
                        continue
                    data = dataset.read(1, window=plan.windows[index], masked=True)
                    stats.read_batches += 1
                    process_batch(plan, int(index), data, accumulator, stats, prune=prune)
                    if accumulator.witness:
                        return accumulator.response(stopped=True)
        else:
            for index in np.argsort(plan.lower_bounds, kind="stable"):
                check_deadline(deadline, cancelled)
                if plan.outside[index] and plan.block_key(plan.windows[index]) not in plan.boundary:
                    continue
                window = plan.windows[index]
                data = np.ma.array(
                    np.zeros((int(window.height), int(window.width))),
                    mask=not assumed_sea(plan.source),
                )
                process_batch(plan, int(index), data, accumulator, stats, prune=prune)
                if accumulator.witness:
                    return accumulator.response(stopped=True)
    check_deadline(deadline, cancelled)
    return accumulator.response()


def observer_inside(request: AreaHorizonRequest) -> bool:
    return bool(shape(request.geometry.model_dump()).covers(Point(*request.observer_coordinates)))


class AreaHorizonService:
    def __init__(self, tile_service: S3TileService, settings: Settings) -> None:
        self.tile_service = tile_service
        self.settings = settings

    async def create(self, request: AreaHorizonRequest) -> AreaHorizonResponse:
        cancelled = Event()
        duration = self.settings.area_horizon_timeout_seconds
        deadline = monotonic() + duration
        stats = ProcessingStats()
        started = monotonic()
        try:
            async with asyncio.timeout(duration):
                points, pieces = await asyncio.to_thread(
                    prepare_area,
                    request,
                    self.settings.area_horizon_max_cells,
                    deadline,
                    cancelled,
                )
                sources: dict[str, NativeTile | UnavailableTile] = {}
                if points is None and await asyncio.to_thread(observer_inside, request):
                    tile_id = point_tile(request.observer_coordinates)
                    sources.update(await self.tile_service.get_native_sources([tile_id]))
                    observer = await asyncio.to_thread(
                        point_evaluations,
                        request,
                        [request.observer_coordinates],
                        sources,
                        deadline,
                        cancelled,
                        polygon=True,
                    )
                    if observer.witness:
                        return observer.response(stopped=True)
                tile_ids = (
                    sorted({point_tile(point) for point in points})
                    if points is not None
                    else list(pieces)
                )
                sources.update(
                    await self.tile_service.get_native_sources(
                        [tile_id for tile_id in tile_ids if tile_id not in sources]
                    )
                )
                return await asyncio.to_thread(
                    process_area,
                    request,
                    points,
                    pieces,
                    sources,
                    self.settings.area_horizon_max_cells,
                    deadline,
                    cancelled,
                    stats=stats,
                )
        except TimeoutError as exc:
            raise AreaHorizonTimeoutError(
                "Area horizon processing timed out", code="area_horizon_timeout"
            ) from exc
        finally:
            cancelled.set()
            logger.info(
                "Area horizon processed: elapsed=%.3fs read_batches=%d "
                "distance_batches=%d pruned_batches=%d",
                monotonic() - started,
                stats.read_batches,
                stats.distance_batches,
                stats.pruned_batches,
            )
