"""Shared test fixtures.

Environment variables pointing at a throw-away directory are set *before* the
application is imported so settings pick them up; each test then starts from
an empty database and an empty uploads directory.
"""

from __future__ import annotations

import os
import tempfile

_TEST_ROOT = tempfile.mkdtemp(prefix="geospatial-api-tests-")
os.environ["GEO_UPLOAD_DIR"] = os.path.join(_TEST_ROOT, "uploads")
os.environ["GEO_DATABASE_PATH"] = os.path.join(_TEST_ROOT, "metadata.sqlite3")
os.environ["GEO_LOG_LEVEL"] = "WARNING"

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.api.files import get_file_service  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.main import app  # noqa: E402

# Expose the throwaway root to tests that need to assert on side effects.
TEST_ROOT = _TEST_ROOT


@pytest.fixture
async def client():
    """Async httpx client wired to the ASGI app (no server needed)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as async_client:
        yield async_client


@pytest.fixture(autouse=True)
def clean_state():
    """Give every test a fresh database and uploads directory."""
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    for path in settings.upload_dir.iterdir():
        if path.is_file():
            path.unlink()
    get_file_service().repository.delete_all()
    yield
