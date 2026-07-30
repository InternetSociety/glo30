from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ViewshedRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "observer_coordinates": [174.2316077, -39.0035668],
                "observer_height_agl_m": 30,
                "target_height_agl_m": 0,
                "radius_m": 10_000,
            }
        }
    )

    observer_coordinates: tuple[float, float] = Field(
        description="Observer position as [longitude, latitude] in decimal degrees."
    )
    observer_height_agl_m: float = Field(ge=0, le=10_000)
    target_height_agl_m: float = Field(default=0, ge=0, le=10_000)
    radius_m: float = Field(gt=0, le=100_000)

    @field_validator("observer_coordinates")
    @classmethod
    def validate_observer_coordinates(cls, coordinates: tuple[float, float]) -> tuple[float, float]:
        longitude, latitude = coordinates
        if not -180 <= longitude <= 180:
            raise ValueError("longitude must be between -180 and 180")
        if not -90 <= latitude <= 90:
            raise ValueError("latitude must be between -90 and 90")
        return coordinates

    @property
    def longitude(self) -> float:
        return self.observer_coordinates[0]

    @property
    def latitude(self) -> float:
        return self.observer_coordinates[1]


class ViewshedProperties(BaseModel):
    observer_height_agl_m: float
    observer_coordinates: tuple[float, float]
    target_height_agl_m: float
    radius_m: float
    dem: Literal["Copernicus GLO-30 DGED"] = "Copernicus GLO-30 DGED"
    visible_area_sq_km: float
    visible_pixel_count: int
    resolution_m: float
    earth_curvature: bool
    refraction_coefficient: float


class GeoJSONGeometry(BaseModel):
    type: Literal["Polygon", "MultiPolygon"]
    coordinates: list[Any]


class GeoJSONFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    properties: ViewshedProperties
    geometry: GeoJSONGeometry
