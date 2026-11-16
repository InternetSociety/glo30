"""Bounded native-centre and boundary candidates for area-horizon v2."""

import math
from dataclasses import dataclass, field
from itertools import pairwise
from threading import Event
from typing import Any

import numpy as np
import shapely
from pyproj import Geod
from rasterio.windows import Window
from shapely.geometry.base import BaseGeometry

from app.schemas.terrain import Coordinate
from app.services.native_dem import (
    BATCH_SIDE,
    candidate_windows,
    check_deadline,
    geocell_geometry,
)
from app.services.s3_tiles import NativeTile, UnavailableTile


@dataclass
class RasterPlan:
    source: NativeTile | UnavailableTile
    transform: Any
    width: int
    height: int
    piece: BaseGeometry
    target: BaseGeometry
    windows: list[Window] = field(default_factory=list)
    lower_bounds: Any = None
    inside: Any = None
    outside: Any = None
    boundary: dict[int, Any] = field(default_factory=dict)

    @property
    def block_columns(self) -> int:
        return math.ceil(self.width / BATCH_SIDE)

    def block_key(self, window: Window) -> int:
        return (
            int(window.row_off) // BATCH_SIDE * self.block_columns
            + int(window.col_off) // BATCH_SIDE
        )


def owned_coordinates(tile_id: str, xs: Any, ys: Any) -> Any:
    west, south, east, north = geocell_geometry(tile_id).bounds
    return (
        (xs >= west)
        & ((xs < east) | ((east == 180) & (xs == east)))
        & (ys >= south)
        & ((ys < north) | ((north == 90) & (ys == north)))
    )


def cell_centres(plan: RasterPlan, window: Window) -> tuple[Any, Any]:
    xs = (
        plan.transform.c
        + (np.arange(int(window.col_off), int(window.col_off + window.width)) + 0.5)
        * plan.transform.a
    )
    ys = (
        plan.transform.f
        + (np.arange(int(window.row_off), int(window.row_off + window.height)) + 0.5)
        * plan.transform.e
    )
    grid_x, grid_y = np.broadcast_arrays(xs[None, :], ys[:, None])
    return grid_x, grid_y


def centre_mask(plan: RasterPlan, window: Window, inside: bool) -> Any:
    if inside:
        return np.ones((int(window.height), int(window.width)), dtype=bool)
    xs, ys = cell_centres(plan, window)
    return shapely.contains_xy(plan.target, xs, ys) & owned_coordinates(plan.source.tile_id, xs, ys)


def boundary_candidates(
    plan: RasterPlan, boundary: BaseGeometry, deadline: float, cancelled: Event
) -> dict[int, Any]:
    """Include grid crossings and interval midpoints, at most half a cell diagonal apart."""
    parts = shapely.get_parts(boundary.intersection(plan.piece))
    while np.any(shapely.get_type_id(parts) >= 4):
        check_deadline(deadline, cancelled)
        parts = shapely.get_parts(parts)
    grouped: dict[int, list[Any]] = {}
    for part in parts:
        xy = shapely.get_coordinates(part)
        if not len(xy):
            continue
        segments = pairwise(xy) if len(xy) > 1 else [(xy[0], xy[0])]
        for start, stop in segments:
            check_deadline(deadline, cancelled)
            c0, r0 = ~plan.transform @ start
            c1, r1 = ~plan.transform @ stop
            times = [np.array([0.0, 1.0])]
            for a, b in ((c0, c1), (r0, r1)):
                if b != a:
                    crossings = np.arange(math.floor(min(a, b)) + 1, math.ceil(max(a, b)))
                    times.append((crossings - a) / (b - a))
            endpoints = np.unique(np.concatenate(times))
            ts = np.sort(np.r_[endpoints, (endpoints[:-1] + endpoints[1:]) / 2])
            points = start + ts[:, None] * (stop - start)
            cols = np.floor((points[:, 0] - plan.transform.c) / plan.transform.a).astype(np.int64)
            rows = np.floor((points[:, 1] - plan.transform.f) / plan.transform.e).astype(np.int64)
            # Preserve the native point sampler's integer-latitude and global-bound rules.
            rows[(points[:, 1] == np.floor(points[:, 1])) & (rows == plan.height)] -= 1
            cols[np.abs(points[:, 0]) == 180] = np.clip(
                cols[np.abs(points[:, 0]) == 180], 0, plan.width - 1
            )
            rows[np.abs(points[:, 1]) == 90] = np.clip(
                rows[np.abs(points[:, 1]) == 90], 0, plan.height - 1
            )
            valid = (
                (cols >= 0)
                & (cols < plan.width)
                & (rows >= 0)
                & (rows < plan.height)
                & owned_coordinates(plan.source.tile_id, points[:, 0], points[:, 1])
            )
            points, rows, cols = points[valid], rows[valid], cols[valid]
            keys = rows // BATCH_SIDE * plan.block_columns + cols // BATCH_SIDE
            order = np.argsort(keys, kind="stable")
            for indices in np.split(order, np.flatnonzero(np.diff(keys[order])) + 1):
                if len(indices):
                    grouped.setdefault(int(keys[indices[0]]), []).append(
                        np.column_stack((points[indices], rows[indices], cols[indices]))
                    )
    return {key: np.concatenate(chunks) for key, chunks in grouped.items()}


def batch_distance_bounds(windows: list[Window], transform: Any, observer: Coordinate) -> Any:
    if not windows:
        return np.empty(0)
    left = np.array([w.col_off for w in windows])
    top = np.array([w.row_off for w in windows])
    right = left + np.array([w.width for w in windows])
    bottom = top + np.array([w.height for w in windows])
    west = np.clip(transform.c + left * transform.a, -180, 180)
    east = np.clip(transform.c + right * transform.a, -180, 180)
    north = np.clip(transform.f + top * transform.e, -90, 90)
    south = np.clip(transform.f + bottom * transform.e, -90, 90)
    # Four observer/corner pairs and both diagonals in one vectorized inversion.
    xs = np.stack((west, east, east, west, west, east))
    ys = np.stack((north, north, south, south, north, north))
    starts_x = np.full_like(xs, observer[0])
    starts_y = np.full_like(ys, observer[1])
    starts_x[4:], starts_y[4:] = np.stack((east, west)), np.stack((south, south))
    _, _, distances = Geod(ellps="WGS84").inv(
        starts_x.ravel(), starts_y.ravel(), xs.ravel(), ys.ravel()
    )
    distances = np.asarray(distances).reshape(6, -1) / 1000
    return np.maximum(0, np.min(distances[:4], axis=0) - np.max(distances[4:], axis=0))


def plan_batches(
    plan: RasterPlan,
    boundary: BaseGeometry,
    observer: Coordinate,
    deadline: float,
    cancelled: Event,
) -> None:
    rectangles = candidate_windows(
        plan.piece, plan.transform, plan.width, plan.height, deadline=deadline, cancelled=cancelled
    )
    keys: set[int] = set()
    for rectangle in rectangles:
        check_deadline(deadline, cancelled)
        for row in range(
            int(rectangle.row_off) // BATCH_SIDE,
            math.ceil((rectangle.row_off + rectangle.height) / BATCH_SIDE),
        ):
            for col in range(
                int(rectangle.col_off) // BATCH_SIDE,
                math.ceil((rectangle.col_off + rectangle.width) / BATCH_SIDE),
            ):
                keys.add(row * plan.block_columns + col)
    plan.boundary = boundary_candidates(plan, boundary, deadline, cancelled)
    keys.update(plan.boundary)
    plan.windows = [
        Window(
            (key % plan.block_columns) * BATCH_SIDE,
            (key // plan.block_columns) * BATCH_SIDE,
            min(BATCH_SIDE, plan.width - (key % plan.block_columns) * BATCH_SIDE),
            min(BATCH_SIDE, plan.height - (key // plan.block_columns) * BATCH_SIDE),
        )
        for key in sorted(keys)
    ]
    plan.lower_bounds = batch_distance_bounds(plan.windows, plan.transform, observer)
    west = np.array([plan.transform.c + w.col_off * plan.transform.a for w in plan.windows])
    north = np.array([plan.transform.f + w.row_off * plan.transform.e for w in plan.windows])
    east = west + np.array([w.width * plan.transform.a for w in plan.windows])
    south = north + np.array([w.height * plan.transform.e for w in plan.windows])
    boxes = shapely.box(west, south, east, north)
    plan.inside = shapely.covers(plan.piece, boxes)
    plan.outside = ~shapely.intersects(plan.piece, boxes)
    check_deadline(deadline, cancelled)
