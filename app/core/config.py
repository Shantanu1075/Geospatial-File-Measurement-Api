"""Application configuration.

All values can be overridden through environment variables prefixed with
``GEO_`` (for example ``GEO_MAX_UPLOAD_SIZE_MB=50``).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the API service."""

    app_name: str = "Geospatial Measurement API"
    app_version: str = "1.0.0"

    # Where uploaded files are stored (created at startup).
    upload_dir: Path = Path("uploads")
    # SQLite database holding file metadata and processed results.
    database_path: Path = Path("geospatial.db")

    # Upload / archive limits.
    max_upload_size_mb: int = Field(default=20, ge=1)
    max_zip_entries: int = Field(default=1000, ge=1)
    max_zip_expanded_mb: int = Field(default=200, ge=1)

    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_prefix="GEO_")

    @property
    def allowed_extensions(self) -> tuple[str, ...]:
        """File extensions accepted by the upload endpoint."""
        return (".kml", ".zip")

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def max_zip_expanded_bytes(self) -> int:
        return self.max_zip_expanded_mb * 1024 * 1024


settings = Settings()
