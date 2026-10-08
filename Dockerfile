# Geospatial Measurement API
#
# pyogrio ships self-contained manylinux wheels with GDAL bundled, so no
# system GDAL/GEOS/PROJ packages are required for the default install. If you
# switch to building Fiona/GDAL from source, install the system libraries
# (libgdal-dev, libgeos-dev, libproj-dev) instead.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Application code only — no secrets, no local databases, no test data.
COPY app ./app

# Runtime directories + non-root user. The ownership fix must happen BEFORE
# the VOLUME declaration, otherwise the volume is initialized from the image
# state prior to chown and the app user cannot write uploads.
RUN mkdir -p /app/uploads \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app

# uploads + SQLite live here; mount a volume in production.
VOLUME ["/app/uploads"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/docs', timeout=4).status == 200 else 1)"

USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
