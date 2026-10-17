import os
import pytest
from httpx import ASGITransport, AsyncClient
from app.main import create_app
from app.api.config import Settings

OPERATOR_TOKEN = "test-operator-token-local-demo-0000000000"


@pytest.fixture(autouse=True)
def isolated_demo_latch(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_LATCH_FILE", str(tmp_path / "stop.json"))


@pytest.fixture
def demo_latch_path(tmp_path):
    return tmp_path / "demo_stop_latch.json"


@pytest.fixture
def demo_settings(demo_latch_path, monkeypatch):
    monkeypatch.setenv("DATA_MODE", "demo")
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("DEMO_LATCH_FILE", str(demo_latch_path))
    return Settings.environment()


@pytest.fixture
async def demo_client(demo_settings):
    app = create_app(settings=demo_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def auth_headers():
    return {"Authorization": f"Bearer {OPERATOR_TOKEN}"}
