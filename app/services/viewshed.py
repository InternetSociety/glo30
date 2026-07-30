import asyncio
import math
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from pyproj import CRS, Transformer
from rasterio.features import shapes
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject
from shapely import make_valid
from shapely.geometry import (
    GeometryCollection,
    MultiPolygon,
    Polygon,
    mapping,
    shape,
)
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union

from app.config import Settings
from app.exceptions import DemCoverageError, RadiusLimitError, ViewshedProcessingError
from app.schemas.viewshed import (
    GeoJSONFeature,
    GeoJSONGeometry,
    ViewshedProperties,
    ViewshedRequest,
)
from app.services.s3_tiles import S3TileService

DEM_NODATA = -9999.0


@dataclass(frozen=True)
class ProcessedViewshed:
    geometry: dict[str, Any]
    visible_pixel_count: int
    visible_area_sq_km: float


class ViewshedProcessor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def process(
        self,
        tile_paths: list[Path],
        request: ViewshedRequest,
    ) -> ProcessedViewshed:
        local_crs = self._local_crs(request.longitude, request.latitude)
        transform_affine, width, height = self._output_grid(request.radius_m)

        with tempfile.TemporaryDirectory(prefix="glo30-viewshed-") as temporary_directory:
            temporary_path = Path(temporary_directory)
            dem_path = temporary_path / "dem.tif"
            visible_path = temporary_path / "visible.tif"

            dem = self._reproject_dem(
                tile_paths,
                local_crs,
                transform_affine,
                width,
                height,
                request.radius_m,
            )
            self._assert_observer_has_dem(dem)
            self._write_dem(dem_path, dem, local_crs, transform_affine)
            self._run_gdal_viewshed(dem_path, visible_path, request)

            with rasterio.open(visible_path) as source:
                visible = source.read(1) == 1
                output_transform = source.transform

            circle = self._circle_mask(width, height, transform_affine, request.radius_m)
            valid_dem = dem != DEM_NODATA
            visible &= circle & valid_dem
            visible_pixel_count = int(np.count_nonzero(visible))
            if visible_pixel_count == 0:
                raise ViewshedProcessingError("The viewshed calculation produced no visible cells")

            geometry = self._polygonize(visible, output_transform)
            visible_area_sq_km = visible_pixel_count * self.settings.dem_resolution_m**2 / 1_000_000
            vertex_budget = max(4, math.ceil(visible_area_sq_km * 10))
            geometry = simplify_to_vertex_budget(
                geometry,
                vertex_budget,
                max_tolerance=request.radius_m * 2,
            )
            wgs84_geometry = transform(
                Transformer.from_crs(local_crs, "EPSG:4326", always_xy=True).transform,
                geometry,
            )
            wgs84_geometry = polygonal_geometry(make_valid(wgs84_geometry))
            if wgs84_geometry.is_empty:
                raise ViewshedProcessingError("The viewshed geometry became empty")

            return ProcessedViewshed(
                geometry=dict(mapping(wgs84_geometry)),
                visible_pixel_count=visible_pixel_count,
                visible_area_sq_km=visible_area_sq_km,
            )

    @staticmethod
    def _local_crs(longitude: float, latitude: float) -> CRS:
        return CRS.from_proj4(
            f"+proj=aeqd +lat_0={latitude} +lon_0={longitude} +datum=WGS84 +units=m +no_defs"
        )

    def _output_grid(self, radius_m: float) -> tuple[Any, int, int]:
        resolution = self.settings.dem_resolution_m
        side = math.ceil((radius_m * 2) / resolution)
        if side % 2 == 0:
            side += 1
        half_extent = side * resolution / 2
        return from_origin(-half_extent, half_extent, resolution, resolution), side, side

    def _reproject_dem(
        self,
        tile_paths: list[Path],
        local_crs: CRS,
        destination_transform: Any,
        width: int,
        height: int,
        radius_m: float,
    ) -> np.ndarray[Any, np.dtype[np.float32]]:
        destination = np.full((height, width), DEM_NODATA, dtype=np.float32)
        if not tile_paths:
            raise DemCoverageError("No GLO-30 tiles cover the requested area")

        try:
            for tile_path in tile_paths:
                with rasterio.open(tile_path) as source:
                    if source.crs is None:
                        raise DemCoverageError(f"GLO-30 tile {tile_path.name} has no CRS")
                    reproject(
                        source=rasterio.band(source, 1),
                        destination=destination,
                        src_transform=source.transform,
                        src_crs=source.crs,
                        src_nodata=source.nodata,
                        dst_transform=destination_transform,
                        dst_crs=local_crs,
                        dst_nodata=DEM_NODATA,
                        resampling=Resampling.bilinear,
                        init_dest_nodata=False,
                        num_threads=2,
                    )
        except (OSError, rasterio.errors.RasterioError) as exc:
            raise ViewshedProcessingError("Unable to read or reproject a GLO-30 tile") from exc

        destination[~self._circle_mask(width, height, destination_transform, radius_m)] = DEM_NODATA
        return destination

    @staticmethod
    def _assert_observer_has_dem(dem: np.ndarray[Any, np.dtype[np.float32]]) -> None:
        observer_row = dem.shape[0] // 2
        observer_column = dem.shape[1] // 2
        if dem[observer_row, observer_column] == DEM_NODATA:
            raise DemCoverageError("The observer coordinate has no GLO-30 elevation coverage")

    @staticmethod
    def _write_dem(
        path: Path,
        dem: np.ndarray[Any, np.dtype[np.float32]],
        crs: CRS,
        affine: Any,
    ) -> None:
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=dem.shape[1],
            height=dem.shape[0],
            count=1,
            dtype="float32",
            crs=crs,
            transform=affine,
            nodata=DEM_NODATA,
            tiled=True,
            compress="deflate",
        ) as destination:
            destination.write(dem, 1)

    def _run_gdal_viewshed(
        self,
        dem_path: Path,
        visible_path: Path,
        request: ViewshedRequest,
    ) -> None:
        curvature_coefficient = (
            1.0 - self.settings.refraction_coefficient if self.settings.earth_curvature else 0.0
        )
        command = [
            self.settings.gdal_viewshed_path,
            "-q",
            "-b",
            "1",
            "-oz",
            str(request.observer_height_agl_m),
            "-tz",
            str(request.target_height_agl_m),
            "-md",
            str(request.radius_m),
            "-cc",
            str(curvature_coefficient),
            "-iv",
            "0",
            "-vv",
            "1",
            "-ov",
            "0",
            "-a_nodata",
            "0",
            "-f",
            "GTiff",
            "-ox",
            "0",
            "-oy",
            "0",
            str(dem_path),
            str(visible_path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.settings.viewshed_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ViewshedProcessingError("GDAL viewshed execution failed") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else ""
            message = "GDAL viewshed execution failed"
            if detail:
                message = f"{message}: {detail}"
            raise ViewshedProcessingError(message)

    @staticmethod
    def _circle_mask(
        width: int,
        height: int,
        affine: Any,
        radius_m: float,
    ) -> np.ndarray[Any, np.dtype[np.bool_]]:
        columns = np.arange(width, dtype=np.float64) + 0.5
        rows = np.arange(height, dtype=np.float64) + 0.5
        x_coordinates = affine.c + columns * affine.a
        y_coordinates = affine.f + rows * affine.e
        return (y_coordinates[:, None] ** 2 + x_coordinates[None, :] ** 2) <= radius_m**2

    @staticmethod
    def _polygonize(visible: np.ndarray[Any, np.dtype[np.bool_]], affine: Any) -> BaseGeometry:
        polygons = [
            shape(geometry)
            for geometry, value in shapes(
                visible.astype(np.uint8),
                mask=visible,
                transform=affine,
                connectivity=8,
            )
            if value == 1
        ]
        if not polygons:
            raise ViewshedProcessingError("Visible cells could not be polygonised")
        geometry = polygonal_geometry(make_valid(unary_union(polygons)))
        if geometry.is_empty:
            raise ViewshedProcessingError("Visible cells could not be polygonised")
        return geometry


class ViewshedService:
    def __init__(
        self,
        tile_service: S3TileService,
        processor: ViewshedProcessor,
        settings: Settings,
    ) -> None:
        self.tile_service = tile_service
        self.processor = processor
        self.settings = settings

    async def create(self, request: ViewshedRequest) -> GeoJSONFeature:
        if request.radius_m > self.settings.max_radius_m:
            raise RadiusLimitError(
                f"radius_m must not exceed {self.settings.max_radius_m:g} metres"
            )
        tile_paths = await self.tile_service.get_tiles(
            request.longitude,
            request.latitude,
            request.radius_m,
        )
        processed = await asyncio.to_thread(self.processor.process, tile_paths, request)
        return GeoJSONFeature(
            properties=ViewshedProperties(
                observer_height_agl_m=request.observer_height_agl_m,
                observer_coordinates=request.observer_coordinates,
                target_height_agl_m=request.target_height_agl_m,
                radius_m=request.radius_m,
                visible_area_sq_km=round(processed.visible_area_sq_km, 6),
                visible_pixel_count=processed.visible_pixel_count,
                resolution_m=self.settings.dem_resolution_m,
                earth_curvature=self.settings.earth_curvature,
                refraction_coefficient=self.settings.refraction_coefficient,
            ),
            geometry=GeoJSONGeometry.model_validate(processed.geometry),
        )


def polygonal_geometry(geometry: BaseGeometry) -> BaseGeometry:
    if isinstance(geometry, Polygon | MultiPolygon):
        return geometry
    if isinstance(geometry, GeometryCollection):
        polygons: list[Polygon] = []
        for part in geometry.geoms:
            polygonal = polygonal_geometry(part)
            if isinstance(polygonal, Polygon):
                polygons.append(polygonal)
            elif isinstance(polygonal, MultiPolygon):
                polygons.extend(polygonal.geoms)
        return unary_union(polygons) if polygons else Polygon()
    return Polygon()


def vertex_count(geometry: BaseGeometry) -> int:
    if isinstance(geometry, Polygon):
        return max(0, len(geometry.exterior.coords) - 1) + sum(
            max(0, len(interior.coords) - 1) for interior in geometry.interiors
        )
    if isinstance(geometry, MultiPolygon):
        return sum(vertex_count(polygon) for polygon in geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        return sum(vertex_count(part) for part in geometry.geoms)
    return 0


def simplify_to_vertex_budget(
    geometry: BaseGeometry,
    vertex_budget: int,
    *,
    max_tolerance: float,
) -> BaseGeometry:
    geometry = polygonal_geometry(make_valid(geometry))
    if vertex_count(geometry) <= vertex_budget:
        return geometry

    low = 0.0
    high = max(max_tolerance, 1.0)
    best: BaseGeometry | None = None

    for _ in range(40):
        midpoint = (low + high) / 2
        candidate = polygonal_geometry(
            make_valid(geometry.simplify(midpoint, preserve_topology=False))
        )
        if candidate.is_empty:
            high = midpoint
        elif vertex_count(candidate) <= vertex_budget:
            best = candidate
            high = midpoint
        else:
            low = midpoint

    if best is not None and vertex_count(best) <= vertex_budget:
        return best

    polygons = [geometry] if isinstance(geometry, Polygon) else list(geometry.geoms)
    largest = max(polygons, key=lambda polygon: polygon.area)
    fallback = polygonal_geometry(largest.envelope)
    if fallback.is_empty or vertex_count(fallback) > vertex_budget:
        raise ViewshedProcessingError("Unable to satisfy the GeoJSON vertex budget")
    return fallback
