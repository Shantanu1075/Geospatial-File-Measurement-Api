# Geospatial Measurement API

A production-style **Python + FastAPI** service that accepts geospatial files (KML and zipped Shapefiles), extracts their features, handles coordinate reference systems (CRS) correctly, and returns **area in square metres** and **length in metres** through a small REST API.

```
POST /api/files/                       upload a .kml or .zip Shapefile
GET  /api/files/{id}/                  file metadata + processing status
GET  /api/files/{id}/measurements/     measurement for every feature
GET  /api/files/{id}/features/         all extracted features (GeoJSON)
GET  /docs                             OpenAPI UI (Swagger)
GET  /redoc                            ReDoc UI
```

---

## Project overview

Upload a geospatial file → the service validates and stores it, reads it with GeoPandas, detects its CRS, selects an appropriate **metre-based projected CRS**, transforms the geometries, computes per-feature measurements (area/length), persists everything in SQLite, and serves it back through typed JSON endpoints.

The critical design point is CRS correctness: area and length are **never** computed on degree-based coordinates (EPSG:4326) and **never** on Web Mercator. See [CRS strategy](#crs-strategy).

## Features

- **KML support** (`.kml`, mixed geometry types in one file)
- **Shapefile ZIP support** (`.zip` containing `.shp/.shx/.dbf/.prj`, handled as one dataset)
- **Feature extraction**: feature id, geometry type, GeoJSON geometry, CRS, attributes
- **Polygon / MultiPolygon → area (m²)**
- **LineString / MultiLineString → length (m)**
- **Point / MultiPoint →** `measurement: null` (no measurement required)
- **CRS detection + automatic projected-CRS selection** (UTM zone of the dataset centroid via PyProj)
- **Meter-based measurements** normalized across geographic, feet-based and Mercator sources
- **Graceful handling** of invalid, empty, null and unsupported geometries (per-feature statuses, never a whole-file crash)
- **Consistent error handling** (`{"detail": "..."}` for every failure, correct HTTP status codes)
- **Secure ZIP extraction** (zip-slip, symlink, zip-bomb and entry-count protection)
- **Upload size limits** (413)
- **Automatic API documentation** (`/docs`, `/redoc`, OpenAPI)
- **Tests**: 58 automated tests (unit + integration + security)
- **Docker** support
- **SQLite persistence** of metadata, status, features and measurements

## Architecture

```
HTTP Request
     ↓
FastAPI Router            (app/api/files.py — HTTP, status codes, response models)
     ↓
File Service              (validation, safe storage, ZIP handling, orchestration)
     ↓
GeoPandas / pyogrio       (reads .kml / .shp → GeoDataFrame)
     ↓
Feature Extraction        (geospatial_service — GeoJSON geometry, properties)
     ↓
CRS Service               (crs_service — detect → choose projected CRS → transform)
     ↓
Measurement Service       (measurement_service — area/length + statuses)
     ↓
Persistence               (models — SQLite: metadata, status, JSON results)
     ↓
API Response              (typed Pydantic schemas)
```

### Project structure

```text
.
├── app/
│   ├── main.py                 # FastAPI app, lifespan, exception handlers, logging
│   ├── api/
│   │   └── files.py            # routes (thin) + service wiring
│   ├── schemas/
│   │   └── file.py             # Pydantic response models
│   ├── services/
│   │   ├── file_service.py     # upload pipeline / orchestration
│   │   ├── geospatial_service.py  # dataset reading, feature extraction
│   │   ├── crs_service.py      # CRS detection, UTM selection, transformation
│   │   └── measurement_service.py  # area/length + measurement statuses
│   ├── models/
│   │   └── file.py             # SQLite repository + FileRecord/FileStatus
│   ├── core/
│   │   ├── config.py           # pydantic-settings (GEO_* env vars)
│   │   └── exceptions.py       # domain errors → HTTP statuses
│   └── utils/
│       └── file_utils.py       # filename sanitizing, secure ZIP extraction, JSON-safe conversion
├── tests/
│   ├── conftest.py             # isolated env + httpx ASGI client
│   ├── fixtures/__init__.py    # programmatic KML/Shapefile/ZIP builders
│   ├── test_upload.py
│   ├── test_file_api.py
│   ├── test_measurements.py
│   ├── test_crs.py
│   └── test_security.py
├── uploads/                    # stored uploads (UUID names, git-ignored)
├── requirements.txt
├── pyproject.toml              # metadata + pytest config
├── Dockerfile
└── README.md
```

## CRS strategy

```text
Geographic CRS (e.g. EPSG:4326)
      ↓
Determine dataset centroid (in lon/lat, antimeridian-safe)
      ↓
Determine UTM zone from centroid (pyproj.database.query_utm_crs_info)
      ↓
Transform geometry to that projected CRS
      ↓
Calculate measurement in metres
```

### Why measuring in EPSG:4326 is wrong

EPSG:4326 coordinates are **degrees**, not metres. `gdf.geometry.area` on geographic data returns values in "square degrees" — meaningless as an area (a square degree near the equator is ~12,000 km², near 60°N it is a third of that). The code never calls `.area`/`.length` while `gdf.crs.is_geographic` is true; the CRS service always projects first.

### Why not Web Mercator (EPSG:3857)

Web Mercator is a cylindrical projection with scale factor `1/cos(φ)`:

- Area is inflated by `1/cos²(φ)` — **≈1.6× at 37°N, ≈4× at 60°N**.
- Distances are distorted by `1/cos(φ)` away from the equator.

It is fine for tiling web maps, but its distortion grows with latitude, so it is a *bad measurement CRS*. This service therefore:

- never uses EPSG:3857 as a default measurement CRS, and
- **excludes Mercator sources (EPSG:3857 / EPSG:3395) from the "already metric → measure as-is" rule** — a dataset authored in 3857 is reprojected to the local UTM zone before measuring. (A test proves a 3857 upload returns geodesic-accurate area, not the ~1.6×-inflated Mercator value.)

### Projected-CRS selection rules (implemented in `crs_service.py`)

| Source CRS                                         | Measurement CRS                                                          |
| -------------------------------------------------- | ------------------------------------------------------------------------ |
| Geographic (4326, NAD83 lon/lat, …)                | UTM zone of the dataset centroid (WGS84), e.g. `-122, 37.5 → EPSG:32610` |
| Projected, **metres** (UTM, State Plane metres, …) | unchanged — no unnecessary transformation                                |
| Projected, **feet** (e.g. EPSG:2227 us-ft)         | UTM zone of the centroid → normalized to metres                          |
| Mercator (3857 / 3395)                             | UTM zone of the centroid (Mercator distorts measurement)                 |
| UTM unavailable (poles, abs(lat) > 84 / −80)       | local Azimuthal Equidistant centred on the data (still metres)           |
| **Missing CRS**                                    | **HTTP 400** — never silently assumed to be 4326                         |

### Missing-CRS decision

A dataset without a CRS (e.g. a Shapefile whose `.prj` was lost) is rejected:

```json
{
  "detail": "The uploaded dataset does not contain a CRS. Measurement calculation requires a known CRS."
}
```

**Why:** guessing EPSG:4326 for coordinate pairs in an unknown projection can silently produce garbage measurements (e.g. State Plane feet interpreted as degrees). Failing loudly with HTTP 400 is safer than being wrong. The only format-specific exception: **KML coordinates are defined by specification as WGS84 lon/lat**, so a KML that reports no CRS gets EPSG:4326 assigned per the format definition.

## Setup

Requires **Python 3.12+**.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run:

```bash
uvicorn app.main:app --reload
```

Then open [http://localhost:8000/docs](http://localhost:8000/docs).

### Configuration (environment variables)

| Variable                  | Default         | Description                         |
| ------------------------- | --------------- | ----------------------------------- |
| `GEO_UPLOAD_DIR`          | `uploads`       | Where uploaded files are stored     |
| `GEO_DATABASE_PATH`       | `geospatial.db` | SQLite database file                |
| `GEO_MAX_UPLOAD_SIZE_MB`  | `20`            | Max upload size (HTTP 413 above it) |
| `GEO_MAX_ZIP_ENTRIES`     | `1000`          | Max entries per ZIP archive         |
| `GEO_MAX_ZIP_EXPANDED_MB` | `200`           | Max total expanded ZIP size         |
| `GEO_LOG_LEVEL`           | `INFO`          | Standard logging level              |

## API examples

### Upload

```bash
curl -X POST \
  http://localhost:8000/api/files/ \
  -F "file=@survey.kml"
```

```bash
curl -X POST \
  http://localhost:8000/api/files/ \
  -F "file=@survey.zip"
```

**201 response:**

```json
{
  "id": "9a568bdb298648909065976556e87538",
  "filename": "survey.kml",
  "feature_count": 3,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "created_at": "2026-10-08T10:30:00Z",
  "error": null
}
```

### File information

```bash
curl http://localhost:8000/api/files/{id}/
```

```json
{
  "id": "9a568bdb298648909065976556e87538",
  "filename": "survey.kml",
  "feature_count": 3,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "created_at": "2026-10-08T10:30:00Z",
  "error": null
}
```

### Measurements

```bash
curl http://localhost:8000/api/files/{id}/measurements/
```

```json
{
  "file_id": "9a568bdb298648909065976556e87538",
  "measurements": [
    {
      "feature_id": 0,
      "geometry_type": "Polygon",
      "measurement": { "type": "area", "value": 980811.66, "unit": "m²" },
      "measurement_status": null
    },
    {
      "feature_id": 1,
      "geometry_type": "LineString",
      "measurement": { "type": "length", "value": 707.19, "unit": "m" },
      "measurement_status": null
    },
    {
      "feature_id": 2,
      "geometry_type": "Point",
      "measurement": null,
      "measurement_status": null
    }
  ]
}
```

### Features

```bash
curl http://localhost:8000/api/files/{id}/features/
```

```json
{
  "file_id": "9a568bdb298648909065976556e87538",
  "features": [
    {
      "feature_id": 0,
      "geometry_type": "Polygon",
      "geometry": {
        "type": "Polygon",
        "coordinates": [[[-122.005, 37.495], [-122.005, 37.505], [-121.995, 37.505], [-121.995, 37.495], [-122.005, 37.495]]]
      },
      "crs": "EPSG:4326",
      "properties": { "name": "Survey Area", "category": "test" }
    }
  ]
}
```

### Errors

All failures share the same shape:

```json
{ "detail": "Unsupported file type. Only .kml and .zip Shapefiles are supported." }
```

| Status | Cause                                                                                                                                          |
| ------ | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `400`  | Unsupported extension, empty upload, invalid ZIP, ZIP without `.shp`, unsafe ZIP entries, invalid KML/dataset, missing CRS, file not processed |
| `404`  | Unknown file id                                                                                                                                |
| `413`  | Upload exceeds `GEO_MAX_UPLOAD_SIZE_MB`                                                                                                        |
| `422`  | Missing/invalid request parts (e.g. no `file` field)                                                                                           |
| `500`  | Unexpected server error (details logged, never returned)                                                                                       |

## Testing

```bash
pytest
```

**58 tests** covering:

- **Upload** — KML ✓, Shapefile ZIP ✓, unsupported extension ✗, empty upload ✗, invalid ZIP ✗, ZIP without `.shp` ✗, invalid KML ✗, oversized upload (413), traversal filenames
- **File API** — metadata for existing files, 404 for unknown ids, features endpoint, persistence check
- **Measurements** — polygon area ≈ geodesic reference (`pytest.approx(..., rel=0.02)`), line length ≈ geodesic, point → `null`, MultiPolygon/MultiLineString totals, unsupported geometry status, invalid geometry repair + `INVALID_GEOMETRY`, unrepairable (NaN) geometry
- **CRS** — 4326 → UTM 10N (EPSG:32610), southern-hemisphere zone, metric source unchanged, feet source normalized to metres, polar fallback, missing CRS → 400, **EPSG:3857 upload measured as ground truth, not Mercator**
- **Security** — zip-slip `../../evil.shp`, absolute-path entry, symlink entry, zip-bomb size cap, entry-count cap, filename sanitizing, no filesystem paths in responses

All fixtures are **generated programmatically** in `tests/fixtures/` — no external GIS data, no manual setup; `pip install -r requirements.txt && pytest` is enough.

Expected values are cross-checked against **geodesic computations** (`pyproj.Geod`) with relative tolerances rather than exact float equality.

## Docker

```bash
docker build -t geospatial-api .
docker run -p 8000:8000 geospatial-api
```

Persist uploads and the database with a volume:

```bash
docker run -p 8000:8000 -v geoapi-data:/app/uploads geospatial-api
```

The image uses `python:3.12-slim`; `pyogrio` wheels bundle GDAL/GEOS/PROJ, so no system GIS packages are required (add `libgdal-dev libgeos-dev libproj-dev` only if you switch to source-built drivers). The container runs as a non-root user and includes a `/docs` healthcheck.

## Design decisions

- **Why GeoPandas** — the standard Python dataframe abstraction for geospatial data; reads KML and Shapefile through one interface (`pyogrio` engine) and gives CRS handling + reprojection (`to_crs`) for free.
- **Why Shapely** — fast GEOS-backed geometry primitives (`area`, `length`, `is_valid`, `make_valid`) used directly by the measurement service.
- **Why PyProj** — authoritative CRS database and transformation engine; `query_utm_crs_info` picks the correct UTM zone from the centroid without hard-coded EPSG codes.
- **Why SQLite** — a single-file database needs zero infrastructure, survives restarts, and is ample for a take-home-scale service. One connection per operation keeps it thread-safe without a pool. PostgreSQL/PostGIS would add operational cost with no benefit at this scale.
- **Why UTM** — a transverse-Mercator zone keeps distortion below ~0.1% for data within the zone: accurate, globally available, and metre-based by definition. The alternative (geodesic per-feature computation) would not generalize to projected sources that are already metric.
- **Why synchronous processing** — upload requests are small and processing is milliseconds-to-seconds; Celery/Redis/background workers would be infrastructure theater. CPU-bound GIS work runs in Starlette's thread pool so the event loop stays responsive. A `PROCESSING` status and repository seam make an async worker a drop-in later change.
- **How invalid geometries are handled** — checked per feature with `is_valid`. Invalid geometries get a conservative repair attempt (`make_valid`, then `buffer(0)`); if repair fails the feature returns `measurement: null, measurement_status: "INVALID_GEOMETRY"` and **the rest of the file still processes**. Valid geometries are never touched.
- **How ZIP security is handled** — no `extractall()`. Every member name is validated (no `..`, no absolute/Windows-drive paths), symlink members are rejected, entry count and expanded size are capped (declared *and* actual bytes), extraction happens only into a temporary directory deleted right after processing, and generated filenames are UUIDs so client names never reach the filesystem.
- **Why UUID ids** — client filenames are untrusted input; ids and storage names are generated server-side, so path traversal through filenames is structurally impossible. Server paths are never present in responses.

## Supported geometry types

| Geometry                     | Measurement                   | Unit |
| ---------------------------- | ----------------------------- | ---- |
| `Polygon`                    | area                          | m²   |
| `MultiPolygon`               | total area                    | m²   |
| `LineString`                 | length                        | m    |
| `MultiLineString`            | total length                  | m    |
| `LinearRing`                 | length                        | m    |
| `Point`                      | none (`null`)                 | —    |
| `MultiPoint`                 | none (`null`)                 | —    |
| `GeometryCollection`, others | none + `UNSUPPORTED_GEOMETRY` | —    |
| null/empty geometry          | none + `EMPTY_GEOMETRY`       | —    |
| invalid, unrepairable        | none + `INVALID_GEOMETRY`     | —    |

## Limitations

- One UTM zone is selected from the dataset centroid; datasets spanning multiple UTM zones (hundreds of km) carry modest distortion at the far edges.
- Processing is synchronous — fine for files up to the 20 MB default limit; swap in a worker queue for very large corpora (the `PROCESSING/COMPLETED/FAILED` states already accommodate it).
- ZIP extraction writes to a temporary directory (cleaned up automatically); extremely large archives are bounded by `GEO_MAX_ZIP_*` limits rather than streamed.
- Measurements assume data in a projected metre CRS is intended to be measured in that CRS (except Mercator); exotic projections keep their native distortion.
