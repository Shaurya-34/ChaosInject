"""Real local HTTP integration test (separate demo target on loopback)."""
import socket
import subprocess
import sys
import time
import pytest
import httpx
from httpx import ASGITransport, AsyncClient
from app.main import create_app
from app.api.config import Settings
from tests.conftest import OPERATOR_TOKEN, auth_headers

TARGET_HOST = "127.0.0.1"
with socket.socket() as free_socket:
    free_socket.bind((TARGET_HOST, 0))
    TARGET_PORT = free_socket.getsockname()[1]
TARGET_URL = f"http://{TARGET_HOST}:{TARGET_PORT}/health"


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) != 0


@pytest.fixture(scope="module")
def demo_target_server():
    if not _port_free(TARGET_HOST, TARGET_PORT):
        yield None
        return
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.demo_target:app",
            "--host",
            TARGET_HOST,
            "--port",
            str(TARGET_PORT),
            "--log-level",
            "warning",
        ],
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1] / "backend"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            response = httpx.get(TARGET_URL, timeout=0.5)
            if response.status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        proc.terminate()
        pytest.skip("Demo target did not become ready on 8090")
    yield proc
    proc.terminate()
    proc.wait(timeout=5)


@pytest.mark.integration_http
@pytest.mark.asyncio
async def test_real_http_target_stays_healthy_and_ninety_second_injection_blocked(
    demo_target_server,
    demo_latch_path,
    monkeypatch,
):
    if demo_target_server is None and not _port_free(TARGET_HOST, TARGET_PORT):
        pytest.skip("Port 8090 already in use; start demo target manually for this test")

    monkeypatch.setenv("DATA_MODE", "demo")
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("DEMO_LATCH_FILE", str(demo_latch_path))
    monkeypatch.setenv("DEMO_TARGET_PORT", str(TARGET_PORT))
    settings = Settings.environment()
    app = create_app(settings=settings)
    transport = ASGITransport(app=app)

    before = httpx.get(TARGET_URL, timeout=2).json()
    assert before["status"] == "healthy"
    assert before["mutations_received"] == 0

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/demo/injection",
            headers=auth_headers(),
            json={"fault_type": "service_pause", "target_id": "demo-target", "duration_seconds": 90},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["rule_id"] == "DURATION_HARD_LIMIT"
        assert payload["executed"] is False
        assert payload["mutations_dispatched"] == 0
        assert payload["evidence_label"] == "LOCAL DEMO"
        assert payload["popup_heading"] == "Error has occurred"
        assert payload["popup_body"] == "Injection blocked before execution"

    after = httpx.get(TARGET_URL, timeout=2).json()
    assert after["status"] == "healthy"
    assert after["mutations_received"] == 0
