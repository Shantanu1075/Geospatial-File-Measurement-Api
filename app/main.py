"""FastAPI application: wiring, logging and consistent error responses.

Every failing response uses the same ``{"detail": "..."}`` JSON shape:
- domain errors (``AppError`` subclasses) carry their own HTTP status,
- request validation failures return 422,
- anything unexpected returns a generic 500 with details logged server-side
  (stack traces are never sent to clients).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.files import get_file_service, router as files_router
from app.core.config import settings
from app.core.exceptions import AppError

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Create runtime directories and initialize persistence at startup."""
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    get_file_service()  # constructs the repository, creating the schema
    logger.info("Starting %s v%s", settings.app_name, settings.app_version)
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "Upload KML / zipped Shapefile datasets and retrieve feature metadata, "
        "GeoJSON geometries and measurements (area in m², length in m) computed "
        "in an automatically selected metre-based projected CRS (UTM zone of "
        "the dataset centroid)."
    ),
    lifespan=lifespan,
    openapi_tags=[
        {
            "name": "files",
            "description": "Upload geospatial files and query their metadata and measurements.",
        }
    ],
)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """Map domain errors onto their HTTP status with a consistent body."""
    logger.info("Rejected %s %s: %s", request.method, request.url.path, exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def request_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Flatten FastAPI's validation errors into the same detail shape."""
    logger.info("Validation error on %s %s: %s", request.method, request.url.path, exc.errors())
    return JSONResponse(
        status_code=422,
        content={"detail": "Request validation failed. Check the request body and parameters."},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all: log the full traceback, return a generic 500."""
    logger.error(
        "Unhandled error on %s %s: %s", request.method, request.url.path, exc, exc_info=exc
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


app.include_router(files_router)
