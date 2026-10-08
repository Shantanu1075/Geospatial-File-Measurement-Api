"""File-system, archive-security and serialization helpers.

Everything in this module that touches client-supplied data is written
defensively: filenames are never trusted, ZIP members are validated before
extraction, and values are normalized to JSON-safe primitives before they can
reach a response body.
"""

from __future__ import annotations

import logging
import math
import re
import stat
import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from app.core.exceptions import (
    InvalidArchiveError,
    MissingShapefileError,
    UnsafeArchiveError,
)

logger = logging.getLogger(__name__)

# Characters kept in client-supplied filenames; everything else is replaced.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._ -]+")
# Windows drive-letter prefix (e.g. "C:evil.shp") — always rejected in archives.
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")

_COPY_CHUNK_BYTES = 1024 * 1024


def sanitize_filename(raw_name: str | None) -> str:
    """Reduce a client-supplied filename to a safe display name.

    SECURITY: strips directories (both ``/`` and ``\\`` separators), removes
    path-traversal sequences, drops leading dots (hidden/relative names) and
    strips control/exotic characters. The result is only ever used for
    display — storage names are generated UUIDs.
    """
    if not raw_name:
        return "upload"
    name = raw_name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_NAME_CHARS.sub("_", name)
    name = name.lstrip(".").strip()
    if not name:
        return "upload"
    return name[:255]


def file_extension(filename: str) -> str:
    """Lower-cased extension of a (already sanitized) filename, e.g. ``.kml``."""
    return Path(filename).suffix.lower()


def save_upload_bytes(data: bytes, directory: Path, file_id: str, extension: str) -> Path:
    """Persist upload bytes under a generated UUID name.

    SECURITY: the storage name is derived from our own UUID, never from the
    client filename, so path traversal through the filename is impossible.
    """
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{file_id}{extension}"
    target.write_bytes(data)
    return target


def _is_unsafe_member_name(name: str) -> bool:
    """True when a ZIP member name could escape the extraction directory."""
    if not name or "\x00" in name:
        return True
    if name.startswith(("/", "\\")) or _WINDOWS_DRIVE.match(name):
        return True  # absolute paths (POSIX or Windows)
    # Normalize separators, then look for parent-directory traversal.
    parts = name.replace("\\", "/").split("/")
    return any(part == ".." for part in parts)


def safe_extract_zip(
    zip_path: Path,
    dest_dir: Path,
    *,
    max_entries: int,
    max_expanded_bytes: int,
) -> Path:
    """Validate and extract a user-supplied ZIP, returning its ``.shp`` member.

    SECURITY notes:
    - Never uses ``ZipFile.extractall()``; every member name is validated so
      entries like ``../../evil.shp`` or ``/abs/path.shp`` cannot escape
      ``dest_dir`` (classic "zip slip").
    - Symlink members are rejected outright.
    - Entry count and total *declared* expanded size are capped, and actual
      bytes written are capped too, to blunt zip bombs.
    - Extraction targets a caller-provided temporary directory that the caller
      deletes afterwards; uploaded archives are never unpacked in place.
    - Nothing is ever executed from the archive.

    Raises :class:`InvalidArchiveError`, :class:`UnsafeArchiveError` or
    :class:`MissingShapefileError` (all HTTP 400).
    """
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning("Rejected invalid ZIP upload %s: %s", zip_path.name, exc)
        raise InvalidArchiveError() from exc

    with zf:
        infos = zf.infolist()

        if len(infos) > max_entries:
            raise UnsafeArchiveError(
                f"The ZIP archive contains too many entries (limit: {max_entries})."
            )
        declared_total = sum(info.file_size for info in infos)
        if declared_total > max_expanded_bytes:
            raise UnsafeArchiveError(
                "The ZIP archive expands beyond the allowed size limit."
            )

        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_resolved = dest_dir.resolve()
        shp_candidates: list[Path] = []
        written = 0

        for info in infos:
            name = info.filename
            if _is_unsafe_member_name(name):
                logger.warning(
                    "Rejected ZIP archive %s: unsafe member name %r",
                    zip_path.name,
                    name,
                )
                raise UnsafeArchiveError()

            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                logger.warning(
                    "Rejected ZIP archive %s: symlink member %r", zip_path.name, name
                )
                raise UnsafeArchiveError(
                    "The ZIP archive contains a symbolic link and was rejected."
                )

            target = dest_dir / name
            # Belt-and-braces: even after name validation, verify resolution.
            if not target.resolve().is_relative_to(dest_resolved):
                logger.warning(
                    "Rejected ZIP archive %s: member %r escapes destination",
                    zip_path.name,
                    name,
                )
                raise UnsafeArchiveError()

            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                while chunk := src.read(_COPY_CHUNK_BYTES):
                    written += len(chunk)
                    if written > max_expanded_bytes:
                        raise UnsafeArchiveError(
                            "The ZIP archive expands beyond the allowed size limit."
                        )
                    out.write(chunk)

            if target.suffix.lower() == ".shp":
                shp_candidates.append(target)

    if not shp_candidates:
        raise MissingShapefileError()

    if len(shp_candidates) > 1:
        # Ambiguous archives: use the first (sorted for determinism) but log it.
        logger.warning(
            "ZIP archive %s contains %d shapefiles; using %s",
            zip_path.name,
            len(shp_candidates),
            shp_candidates[0].name,
        )
    return sorted(shp_candidates)[0]


def geometry_to_geojson(geom: BaseGeometry | None) -> dict[str, Any] | None:
    """Convert a Shapely geometry to a JSON-serializable GeoJSON mapping.

    Returns ``None`` for null geometries. Empty geometries become valid GeoJSON
    with empty coordinate arrays. Shapely objects themselves are never returned
    from the API.
    """
    if geom is None:
        return None
    return json_safe(mapping(geom))


def json_safe(value: Any) -> Any:
    """Recursively convert a value into JSON-compatible primitives.

    Handles numpy/pandas scalars, timestamps, NaN/NA/NaT, bytes and nested
    containers so that attribute values from any driver can be serialized.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    # numpy scalars (np.int64, np.bool_, ...) expose a 0-d .item().
    item = getattr(value, "item", None)
    if callable(item) and getattr(value, "ndim", 1) == 0:
        return json_safe(item())
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if value != value:  # any remaining NaN-like object (pd.NA handled above)
        return None
    return str(value)
