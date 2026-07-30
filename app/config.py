from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
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

    dem_resolution_m: float = 30.0
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
