"""Native DEM API contracts. Coordinates always use longitude, latitude order."""

from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from shapely.geometry import shape

Longitude = Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)]
Latitude = Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)]
Coordinate = tuple[Longitude, Latitude]
Ring = Annotated[list[Coordinate], Field(min_length=4)]


class PolygonGeometry(BaseModel):
    type: Literal["Polygon"]
    coordinates: Annotated[list[Ring], Field(min_length=1)]


class MultiPolygonGeometry(BaseModel):
    type: Literal["MultiPolygon"]
    coordinates: Annotated[list[Annotated[list[Ring], Field(min_length=1)]], Field(min_length=1)]


class PointGeometry(BaseModel):
    type: Literal["Point"]
    coordinates: Coordinate


class MultiPointGeometry(BaseModel):
    type: Literal["MultiPoint"]
    coordinates: Annotated[list[Coordinate], Field(min_length=1)]


TerrainGeometry = Annotated[
    PolygonGeometry | MultiPolygonGeometry | PointGeometry | MultiPointGeometry,
    Field(discriminator="type"),
]


class ElevationRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "coordinates": [[174.2316077, -39.0035668], [174.2317, -39.0035]],
            }
        }
    )

    coordinates: Annotated[list[Coordinate], Field(min_length=1, max_length=1000)]


class TileProvenance(BaseModel):
    tile_id: str
    product_id: str
    sha256: str


class DemProvenance(BaseModel):
    dem: Literal["Copernicus GLO-30 DGED"] = "Copernicus GLO-30 DGED"
    horizontal_crs: Literal["EPSG:4326"] = "EPSG:4326"
    height_processing: Literal["native DEM heights; no vertical transformation"] = (
        "native DEM heights; no vertical transformation"
    )
    tiles: list[TileProvenance]


class TerrainProvenance(DemProvenance):
    sampling: Literal["native_cell"]
    processor_version: Literal["elevation-v1"]


class AreaHorizonProvenance(DemProvenance):
    sampling: Literal["native_cell_centre", "native_cell"]
    processor_version: Literal["area-horizon-v2"] = "area-horizon-v2"
    tolerance_km: float
    sea_level_assumed_tiles: list[str]


class ElevationResult(BaseModel):
    coordinates: Coordinate
    status: Literal["ok", "unavailable"]
    elevation_m: float | None
    reason: Literal["dem_unavailable"] | None


class ElevationResponse(BaseModel):
    results: list[ElevationResult]
    provenance: TerrainProvenance


class AreaHorizonRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "observer_coordinates": [174.2316077, -39.0035668],
                "observer_antenna_height_asl_m": 300,
                "target_height_agl_m": 2,
                "effective_earth_radius_factor": 1.3333333333333333,
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [174.2315, -39.0037],
                            [174.2317, -39.0037],
                            [174.2317, -39.0035],
                            [174.2315, -39.0035],
                            [174.2315, -39.0037],
                        ]
                    ],
                },
            }
        }
    )

    observer_coordinates: Coordinate
    observer_antenna_height_asl_m: float = Field(ge=-500, le=12000, allow_inf_nan=False)
    target_height_agl_m: float = Field(default=2, ge=0, le=3000, allow_inf_nan=False)
    effective_earth_radius_factor: float = Field(default=4 / 3, gt=0, le=10, allow_inf_nan=False)
    geometry: TerrainGeometry

    @model_validator(mode="after")
    def validate_geometry(self) -> Self:
        geometry = self.geometry
        if isinstance(geometry, PolygonGeometry | MultiPolygonGeometry):
            polygons = (
                [geometry.coordinates]
                if isinstance(geometry, PolygonGeometry)
                else geometry.coordinates
            )
            for polygon in polygons:
                for ring in polygon:
                    if ring[0] != ring[-1]:
                        raise ValueError("Polygon rings must be closed")
                    if any(abs(a[0] - b[0]) > 180 for a, b in pairwise(ring)):
                        raise ValueError("Antimeridian-crossing polygons are not supported")
            parsed = shape(geometry.model_dump())
            if parsed.is_empty or not parsed.is_valid:
                raise ValueError("Geometry must be valid and nonempty")
        return self


class HorizonEvidence(BaseModel):
    coordinates: Coordinate
    elevation_m: float
    distance_km: float
    horizon_km: float


class AreaHorizonResponse(BaseModel):
    outcome: Literal["within_horizon", "beyond_horizon", "indeterminate"]
    observer_coordinates: Coordinate
    observer_antenna_height_asl_m: float
    target_height_agl_m: float
    effective_earth_radius_factor: float
    coverage_complete: bool
    evaluated_cells: int
    unavailable_cells: int
    evidence: HorizonEvidence | None
    reason: Literal["incomplete_dem", "stopped_at_witness"] | None
    provenance: AreaHorizonProvenance
