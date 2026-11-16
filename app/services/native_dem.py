"""Windowed native DEM access, without viewshed resampling or synthetic terrain."""

import hashlib
import math
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import pairwise
from threading import Event
from time import monotonic
from typing import Any

import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.windows import Window
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry

from app.exceptions import AreaHorizonTimeoutError, TerrainLimitError
from app.schemas.terrain import Coordinate, TileProvenance
from app.services.s3_tiles import NativeTile, copernicus_geocell, geocell_center

BATCH_SIDE = 64


def check_deadline(deadline: float = math.inf, cancelled: Event | None = None) -> None:
    if monotonic() >= deadline or (cancelled is not None and cancelled.is_set()):
        raise AreaHorizonTimeoutError(
            "Area horizon processing timed out", code="area_horizon_timeout"
        )


def point_tile(coordinates: Coordinate) -> str:
    longitude, latitude = coordinates
    return copernicus_geocell(min(89, math.floor(latitude)), min(179, math.floor(longitude)))


def cell_index(source: Any, coordinates: Coordinate) -> tuple[int, int] | None:
    longitude, latitude = coordinates
    row, col = source.index(longitude, latitude, op=math.floor)
    # Integer latitude belongs to the northern geocell. With area-registered test
    # rasters its southern edge is exclusive, so retain that geocell's last row.
    if latitude == math.floor(latitude) and row == source.height:
        row -= 1
    if longitude in (-180, 180):
        col = min(source.width - 1, max(0, col))
    if latitude in (-90, 90):
        row = min(source.height - 1, max(0, row))
    return (row, col) if 0 <= row < source.height and 0 <= col < source.width else None


@contextmanager
def open_native(
    tile: NativeTile,
    deadline: float = math.inf,
    cancelled: Event | None = None,
) -> Iterator[tuple[Any, TileProvenance]]:
    # Compose runs on Linux. GDAL and hashing open the same inode even if a concurrent
    # download atomically replaces the cache pathname during this request.
    with tile.path.open("rb") as raw:
        digest = hashlib.sha256()
        while block := raw.read(1024 * 1024):
            check_deadline(deadline, cancelled)
            digest.update(block)
        raw.seek(0)
        with rasterio.open(f"/proc/self/fd/{raw.fileno()}") as source:
            yield (
                source,
                TileProvenance(
                    tile_id=tile.tile_id, product_id=tile.product_id, sha256=digest.hexdigest()
                ),
            )


def sample_points(
    coordinates: list[Coordinate],
    sources: dict[str, NativeTile | None],
    deadline: float = math.inf,
    cancelled: Event | None = None,
) -> tuple[list[float | None], list[TileProvenance]]:
    values: list[float | None] = [None] * len(coordinates)
    groups: dict[str, list[int]] = {}
    for index, coordinate in enumerate(coordinates):
        groups.setdefault(point_tile(coordinate), []).append(index)
    provenance = []
    for tile_id, indices in sorted(groups.items()):
        check_deadline(deadline, cancelled)
        tile = sources[tile_id]
        if tile is None:
            continue
        with open_native(tile, deadline, cancelled) as (source, metadata):
            provenance.append(metadata)
            for index in indices:
                check_deadline(deadline, cancelled)
                position = cell_index(source, coordinates[index])
                if position is None:
                    continue
                row, col = position
                value = source.read(1, window=Window(col, row, 1, 1), masked=True)[0, 0]
                if not np.ma.is_masked(value) and np.isfinite(value):
                    values[index] = float(value)
    return values, provenance


def geocell_geometry(tile_id: str) -> BaseGeometry:
    longitude, latitude = geocell_center(tile_id)
    return box(longitude - 0.5, latitude - 0.5, longitude + 0.5, latitude + 0.5)


def nominal_grid(tile_id: str) -> tuple[Any, int, int]:
    longitude, latitude = geocell_center(tile_id)
    band = max(0, abs(latitude) - 0.5)
    spacing = next(
        spacing
        for upper, spacing in [(50, 1), (60, 1.5), (70, 2), (80, 3), (85, 5), (91, 10)]
        if band < upper
    )
    dx, dy = spacing / 3600, 1 / 3600
    return (
        from_origin(longitude - 0.5 - dx / 2, latitude + 0.5 + dy / 2, dx, dy),
        round(3600 / spacing) + 1,
        3601,
    )


def geometry_tiles(
    geometry: BaseGeometry, deadline: float, cancelled: Event
) -> dict[str, BaseGeometry]:
    pieces: dict[str, list[BaseGeometry]] = {}
    components = list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]
    for component in components:
        west, south, east, north = component.bounds
        for y in range(max(-90, math.floor(south)), min(89, math.floor(north)) + 1):
            for x in range(max(-180, math.floor(west)), min(179, math.floor(east)) + 1):
                check_deadline(deadline, cancelled)
                tile_id = copernicus_geocell(y, x)
                piece = component.intersection(geocell_geometry(tile_id))
                if not piece.is_empty:
                    pieces.setdefault(tile_id, []).append(piece)
    from shapely import union_all

    result = {}
    for tile_id, parts in sorted(pieces.items()):
        check_deadline(deadline, cancelled)
        result[tile_id] = union_all(parts)
    return result


def candidate_windows(
    geometry: BaseGeometry,
    transform: Any,
    width: int,
    height: int,
    *,
    deadline: float = math.inf,
    cancelled: Event | None = None,
    max_cells: int | None = None,
) -> list[Window]:
    """Sweep component row events with one row of coverage memory."""
    check_deadline(deadline, cancelled)
    components = list(geometry.geoms) if hasattr(geometry, "geoms") else [geometry]
    events: dict[int, list[tuple[int, int, int]]] = {}
    for component in components:
        check_deadline(deadline, cancelled)
        west, south, east, north = component.bounds
        c0, r0 = ~transform @ (west, north)
        c1, r1 = ~transform @ (east, south)
        left, top = max(0, math.ceil(c0) - 1), max(0, math.ceil(r0) - 1)
        right, bottom = min(width, math.floor(c1) + 1), min(height, math.floor(r1) + 1)
        if right > left and bottom > top:
            events.setdefault(top, []).append((left, right, 1))
            events.setdefault(bottom, []).append((left, right, -1))
    rows = sorted(events)
    coverage = np.zeros(width + 1, dtype=np.int64)
    windows: list[Window] = []
    count = 0
    for top, bottom in pairwise(rows):
        check_deadline(deadline, cancelled)
        for left, right, delta in events[top]:
            coverage[left] += delta
            coverage[right] -= delta
        covered = np.cumsum(coverage[:-1]) > 0
        count += int(np.count_nonzero(covered)) * (bottom - top)
        if max_cells is not None:
            enforce_cell_limit(count, max_cells)
        starts = np.flatnonzero(covered & ~np.r_[False, covered[:-1]])
        stops = np.flatnonzero(covered & ~np.r_[covered[1:], False]) + 1
        windows.extend(
            Window(int(left), top, int(right - left), bottom - top)
            for left, right in zip(starts, stops, strict=True)
        )
    return windows


def enforce_cell_limit(count: int, maximum: int) -> None:
    if count > maximum:
        raise TerrainLimitError(
            "Area horizon candidate-cell limit exceeded", code="area_horizon_limit"
        )


def batches(windows: list[Window]) -> Iterator[Window]:
    for window in windows:
        for row in range(int(window.row_off), int(window.row_off + window.height), BATCH_SIDE):
            for col in range(int(window.col_off), int(window.col_off + window.width), BATCH_SIDE):
                yield Window(
                    col,
                    row,
                    min(BATCH_SIDE, window.col_off + window.width - col),
                    min(BATCH_SIDE, window.row_off + window.height - row),
                )
