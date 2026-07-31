from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = "sqlite+aiosqlite:////app/data/app.db"
    data_dir: Path = Path("/app/data")
    tile_cache_dir: Path = Path("/app/data/tiles")

    secret_key: SecretStr = SecretStr("change-this-secret-before-production")
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7
    cookie_secure: bool = False

    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None
    s3_host_base: str = "eodata.dataspace.copernicus.eu"
    # Retained because this is the name used by the supplied environment file.
    s3_host_bucket: str = "eodata.dataspace.copernicus.eu"
    s3_bucket_name: str = "eodata"
    copernicus_catalogue_url: str = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
    glo30_s3_prefix: str | None = None
    tile_cache_expiry_days: int = 30

    # This interval is used only to sample the geodesic request boundary when selecting
    # one-degree source tiles. A smaller value makes coverage checks more conservative but does
    # not add detail to the output polygon. It must be greater than 0 and at most 360 degrees.
    coverage_boundary_sample_interval_degrees: float = Field(default=0.5, gt=0, le=360)

    # Output-shape tuning. Override any of these names in .env and restart the app.
    #
    # The raster grid is the first limit on output detail. Smaller cells create a finer viewshed
    # and polygon outline, but memory and processing grow approximately with the inverse square
    # of this value. GLO-30's native detail is about 30 m, so values below 30 m interpolate the
    # existing terrain rather than adding new terrain measurements.
    dem_resolution_m: float = Field(default=30.0, gt=0)

    # Controls how GLO-30 elevations are interpolated onto the local raster grid. Bilinear is a
    # good default for continuous elevation data. Cubic variants or Lanczos may look smoother,
    # but can overshoot elevations and therefore alter line-of-sight results.
    dem_resampling_method: Literal["nearest", "bilinear", "cubic", "cubic_spline", "lanczos"] = (
        "bilinear"
    )

    # The simplifier's global vertex budget is:
    #   max(geometry_min_vertex_budget,
    #       ceil(visible_area_sq_km * geometry_vertices_per_sq_km))
    # Increasing the density is the main way to retain curved edges and detail. It also increases
    # response size. The budget is shared by every polygon and hole in a result, not applied to
    # each ring independently.
    geometry_vertices_per_sq_km: float = Field(default=100.0, ge=0)
    geometry_min_vertex_budget: int = Field(default=8, ge=4)

    # Rasterio supports connectivity 4 or 8 when joining visible raster cells. Eight joins cells
    # that touch diagonally and usually produces fewer fragmented polygons; four keeps them apart.
    geometry_polygon_connectivity: Literal[4, 8] = 8

    # False matches the existing simplification behaviour and permits small components or holes
    # to collapse when necessary to meet the global budget. True protects topology more strongly,
    # potentially retaining more components but making a tight budget harder to satisfy.
    geometry_simplification_preserve_topology: bool = False

    # Simplification uses a binary search for the smallest tolerance that meets the vertex budget.
    # More iterations improve numerical precision but normally do not add visible detail; 40 is
    # already ample. The tolerance search ceiling is request radius multiplied by the multiplier.
    # Increase the multiplier only if unusually complex geometry cannot meet its budget.
    geometry_simplification_search_iterations: int = Field(default=40, ge=1)
    geometry_simplification_max_tolerance_radius_multiplier: float = Field(default=2.0, gt=0)

    max_radius_m: float = 100_000.0
    earth_curvature: bool = True
    refraction_coefficient: float = 1.0 / 7.0
    gdal_viewshed_path: str = "gdal_viewshed"
    viewshed_timeout_seconds: int = 300

    @property
    def s3_endpoint_url(self) -> str:
        host = self.s3_host_base or self.s3_host_bucket
        if host.startswith(("http://", "https://")):
            return host.rstrip("/")
        return f"https://{host.rstrip('/')}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
