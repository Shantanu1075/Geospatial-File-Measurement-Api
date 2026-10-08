"""CRS tests: geographic → projected selection, units, missing-CRS handling.

Key guarantees verified here:
- EPSG:4326 input selects a UTM zone based on the dataset centroid.
- The measurement CRS is always projected and expressed in metres.
- Sources already projected in metres are used as-is; feet-based sources are
  normalized to a metre-based CRS.
- Datasets without a CRS are rejected with HTTP 400 (never assumed 4326).
- Measurements are NOT taken in Web Mercator (area would be inflated ~1/cos²φ).
"""

from __future__ import annotations

import geopandas as gpd
import pytest
from pyproj import CRS, Geod
from shapely.geometry import Point

from app.core.exceptions import MissingCRSError
from app.services.crs_service import CrsService, is_metric_crs
from tests.fixtures import (
    SQUARE_POLYGON,
    polygon_gdf,
    shapefile_zip_bytes,
    upload_multipart,
)

GEOD = Geod(ellps="WGS84")
service = CrsService()


# ------------------------------------------------------------------ unit level

def test_geographic_crs_maps_to_utm_zone_of_centroid():
    crs = service.select_measurement_crs(
        CRS.from_epsg(4326), (-122.005, 37.495, -121.995, 37.505)
    )
    # lon -122, lat 37.5 → UTM zone 10N → EPSG:32610
    assert crs.to_epsg() == 32610
    assert crs.is_projected
    assert is_metric_crs(crs)


def test_southern_hemisphere_utm_selected():
    crs = service.select_measurement_crs(CRS.from_epsg(4326), (-46.1, -23.1, -45.9, -22.9))
    assert crs.to_epsg() == 32723  # zone 23S
    assert is_metric_crs(crs)


def test_projected_metric_source_is_not_retransformed():
    source = CRS.from_epsg(32610)
    bounds = (500_000.0, 4_150_000.0, 500_800.0, 4_150_700.0)
    chosen = service.select_measurement_crs(source, bounds)
    assert chosen == source


def test_feet_based_projected_source_is_normalized_to_metres():
    # EPSG:2227 = NAD83 / California zone 4 (US survey feet).
    source = CRS.from_epsg(2227)
    assert not is_metric_crs(source)
    # Roughly San Francisco in that CRS (feet).
    chosen = service.select_measurement_crs(source, (5_400_000.0, 2_100_000.0, 5_410_000.0, 2_110_000.0))
    assert chosen.is_projected
    assert is_metric_crs(chosen)
    assert chosen.to_epsg() != 2227


def test_polar_area_falls_back_to_local_metric_projection():
    # UTM does not exist above 84°N — expect a local metre-based fallback.
    crs = service.select_measurement_crs(CRS.from_epsg(4326), (-45.0, 88.0, -44.0, 89.0))
    assert crs.is_projected
    assert is_metric_crs(crs)


def test_missing_crs_raises_domain_error():
    gdf = gpd.GeoDataFrame(geometry=[Point(0, 0)], crs=None)
    with pytest.raises(MissingCRSError) as exc_info:
        service.require_crs(gdf)
    assert "does not contain a CRS" in exc_info.value.detail


# --------------------------------------------------------------- API level

async def test_missing_crs_shapefile_returns_400(client):
    """A zip without .prj has no CRS: 400, not a silent EPSG:4326 guess."""
    content = shapefile_zip_bytes(polygon_gdf(), include_prj=False)
    response = await client.post("/api/files/", files=upload_multipart("noprj.zip", content))
    assert response.status_code == 400
    assert response.json()["detail"] == (
        "The uploaded dataset does not contain a CRS. "
        "Measurement calculation requires a known CRS."
    )


async def test_uploaded_kml_reports_source_crs_and_metric_area(client):
    from tests.fixtures import kml_bytes

    response = await client.post(
        "/api/files/", files=upload_multipart("survey.kml", kml_bytes())
    )
    assert response.status_code == 201
    file_id = response.json()["id"]

    info = await client.get(f"/api/files/{file_id}/")
    assert info.json()["crs"] == "EPSG:4326"

    measurements = await client.get(f"/api/files/{file_id}/measurements/")
    area = measurements.json()["measurements"][0]["measurement"]["value"]
    expected = abs(GEOD.geometry_area_perimeter(SQUARE_POLYGON)[0])
    # Result in square metres (metres after projection), close to geodesic truth.
    assert area == pytest.approx(expected, rel=0.02)


async def test_web_mercator_source_is_not_measured_in_mercator(client):
    """EPSG:3857 input: measured area must match ground truth, not Mercator."""
    gdf = polygon_gdf().to_crs("EPSG:3857")
    content = shapefile_zip_bytes(gdf)
    response = await client.post("/api/files/", files=upload_multipart("mercator.zip", content))
    assert response.status_code == 201, response.text
    file_id = response.json()["id"]
    assert response.json()["crs"] == "EPSG:3857"

    measurements = await client.get(f"/api/files/{file_id}/measurements/")
    area = measurements.json()["measurements"][0]["measurement"]["value"]

    geodesic_area = abs(GEOD.geometry_area_perimeter(SQUARE_POLYGON)[0])
    mercator_area = gdf.geometry.area.iloc[0]  # the *wrong* number, ~1.6× too big

    # Correct: close to geodesic truth.
    assert area == pytest.approx(geodesic_area, rel=0.02)
    # Proof it wasn't measured in Mercator: Mercator value is far off.
    assert abs(area - mercator_area) / mercator_area > 0.3
