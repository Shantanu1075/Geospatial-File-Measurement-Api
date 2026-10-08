"""Domain errors that map onto HTTP responses.

Services raise these instead of FastAPI responses so the business logic stays
framework-independent. ``app.main`` registers handlers that translate them
into consistent ``{"detail": ...}`` JSON bodies. Details are written to be safe
for clients: they never contain stack traces or server file-system paths
(technical details are logged server-side instead).
"""

from __future__ import annotations


class AppError(Exception):
    """Base class for expected, client-visible errors."""

    status_code: int = 400
    default_detail: str = "The request could not be processed."

    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail or self.default_detail
        super().__init__(self.detail)


class UnsupportedFileTypeError(AppError):
    default_detail = (
        "Unsupported file type. Only .kml and .zip Shapefiles are supported."
    )


class EmptyUploadError(AppError):
    default_detail = "The uploaded file is empty."


class FileTooLargeError(AppError):
    status_code = 413
    default_detail = "The uploaded file is too large."


class ArchiveError(AppError):
    """Base class for ZIP archive rejections (HTTP 400)."""


class InvalidArchiveError(ArchiveError):
    default_detail = "The uploaded file is not a valid ZIP archive."


class UnsafeArchiveError(ArchiveError):
    default_detail = (
        "The ZIP archive contains unsafe or suspicious entries and was rejected."
    )


class MissingShapefileError(ArchiveError):
    default_detail = "The ZIP archive does not contain a Shapefile (.shp) file."


class DatasetReadError(AppError):
    default_detail = "The geospatial dataset could not be read."


class KMLParseError(DatasetReadError):
    default_detail = "The KML file could not be parsed."


class MissingCRSError(AppError):
    default_detail = (
        "The uploaded dataset does not contain a CRS. "
        "Measurement calculation requires a known CRS."
    )


class CoordinateError(AppError):
    default_detail = (
        "The dataset contains coordinates that are invalid for the declared CRS."
    )


class FileNotProcessedError(AppError):
    default_detail = "The file has not been processed successfully."


class FileNotFound(AppError):
    status_code = 404
    default_detail = "The requested file was not found."


class ProcessingError(AppError):
    """Unexpected failure while processing a stored file (HTTP 500)."""

    status_code = 500
    default_detail = "File processing failed due to an internal error."
