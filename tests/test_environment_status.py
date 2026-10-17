"""GET /api/v1/chaos/environment: the facts behind the dashboard's first-run checklist. No external network."""

import http.server
import inspect
import json
import threading

import pytest
from fastapi.testclient import TestClient

from app.adapters.fixtures import fixture_ports
from app.api import chaos_runs
from app.api.config import Settings
from app.main import create_app
from chaos import hooks

TOKEN = "env-status-test-token-0000000000000"


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch, tmp_path):
    monkeypatch.setitem(chaos_runs._env_cache, "value", None)
    monkeypatch.setitem(chaos_runs._env_cache, "at", 0.0)
    monkeypatch.setattr(hooks, "RESULTS_DIR", tmp_path)


def client(enabled=False):
    app = create_app(Settings(operator_token=TOKEN, chaos_execution_enabled=enabled), fixture_ports())
    return TestClient(app, headers={"Authorization": "Bearer " + TOKEN})


def scripted(monkeypatch, answers):
    """Replace the probe; ``answers`` maps a URL fragment to (answered, status)."""
    calls = []

    def fake(url, timeout=1.5):
        calls.append(url)
        for fragment, answer in answers.items():
            if fragment in url:
                return answer
        return (False, None)

    monkeypatch.setattr(chaos_runs, "_probe", fake)
    return calls


def test_everything_up(monkeypatch):
    scripted(monkeypatch, {":5000": (True, 200), ":9090": (True, 200)})
    with client(enabled=True) as c:
        body = c.get("/api/v1/chaos/environment").json()
    assert body["execution_enabled"] is True and body["runs_saved"] == 0
    assert body["sandbox"]["reachable"] is True and body["sandbox"]["status"] == 200 and ":5000" in body["sandbox"]["url"]
    assert body["prometheus"] == {"reachable": True, "status": 200}


def test_sandbox_down_and_prometheus_down_are_reported_separately(monkeypatch):
    scripted(monkeypatch, {":9090": (True, 200)})
    with client() as c:
        body = c.get("/api/v1/chaos/environment").json()
    assert body["execution_enabled"] is False
    assert body["sandbox"]["reachable"] is False and body["sandbox"]["status"] is None
    assert body["prometheus"]["reachable"] is True
    chaos_runs._env_cache["value"] = None
    scripted(monkeypatch, {":5000": (True, 200)})
    with client() as c:
        assert c.get("/api/v1/chaos/environment").json()["prometheus"]["reachable"] is False


def test_a_5xx_answer_still_counts_as_reachable_with_its_status(monkeypatch):
    scripted(monkeypatch, {":5000": (True, 503), ":9090": (True, 200)})
    with client() as c:
        sandbox = c.get("/api/v1/chaos/environment").json()["sandbox"]
    assert sandbox["reachable"] is True and sandbox["status"] == 503


def test_runs_saved_counts_the_results(monkeypatch):
    scripted(monkeypatch, {})
    for name in ("a", "b"):
        hooks.save_result({"experiment": {"name": name, "target": "t", "fault": "container_stop"}, "baseline": [], "during": [],
                           "recovery": [], "stop_reason": "x", "recovered": True, "recovery_s": 1.0, "fault_started": 1.0, "fault_ended": 2.0})
    with client() as c:
        assert c.get("/api/v1/chaos/environment").json()["runs_saved"] == 2


def test_only_this_machine_is_probed_for_the_sandbox(monkeypatch, tmp_path):
    exp_dir = tmp_path / "experiments"
    exp_dir.mkdir()
    (exp_dir / "remote.json").write_text(json.dumps({"name": "r", "target": "hotel-sandbox-geo-1", "fault": "container_stop",
                                                    "duration_s": 5, "probe_url": "http://example.com/hotels"}))
    monkeypatch.setattr(chaos_runs, "EXPERIMENTS_DIR", exp_dir)
    calls = scripted(monkeypatch, {":9090": (True, 200)})
    with client() as c:
        body = c.get("/api/v1/chaos/environment").json()
    assert body["sandbox"] == {"url": None, "reachable": None, "status": None}
    assert all("example.com" not in url for url in calls)


def test_a_non_http_url_is_never_fetched(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("urlopen must not be called for a non-http URL")

    monkeypatch.setattr(chaos_runs.urllib.request, "urlopen", boom)
    assert chaos_runs._probe("file:///etc/passwd") == (False, None)
    assert chaos_runs._probe("ftp://127.0.0.1/x") == (False, None)


def test_probes_are_shared_for_a_few_seconds(monkeypatch):
    calls = scripted(monkeypatch, {":5000": (True, 200), ":9090": (True, 200)})
    now = [5000.0]
    monkeypatch.setattr(chaos_runs.time, "time", lambda: now[0])
    with client() as c:
        c.get("/api/v1/chaos/environment")
        first = len(calls)
        c.get("/api/v1/chaos/environment")
        assert len(calls) == first  # served from the cache
        now[0] += chaos_runs.ENV_CACHE_S + 1
        c.get("/api/v1/chaos/environment")
        assert len(calls) == 2 * first


def test_execution_enabled_always_reflects_the_current_setting_even_from_the_cache(monkeypatch):
    scripted(monkeypatch, {":5000": (True, 200), ":9090": (True, 200)})
    with client(enabled=False) as c:
        assert c.get("/api/v1/chaos/environment").json()["execution_enabled"] is False
    with client(enabled=True) as c:
        assert c.get("/api/v1/chaos/environment").json()["execution_enabled"] is True


def test_the_route_needs_the_token_is_read_only_and_runs_in_a_worker_thread(monkeypatch):
    scripted(monkeypatch, {})
    with client() as c:
        assert c.get("/api/v1/chaos/environment", headers={"Authorization": "bad"}).status_code == 401
        assert c.post("/api/v1/chaos/environment").status_code == 405
    route = next(r for r in chaos_runs.create_router(False).routes if r.path.endswith("/environment"))
    assert not inspect.iscoroutinefunction(route.endpoint)  # sync def, so FastAPI uses a worker thread


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(500 if self.path == "/boom" else 200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def test_the_real_probe_against_a_local_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        assert chaos_runs._probe(f"http://127.0.0.1:{port}/") == (True, 200)
        assert chaos_runs._probe(f"http://127.0.0.1:{port}/boom") == (True, 500)
    finally:
        server.shutdown()
        server.server_close()
    assert chaos_runs._probe(f"http://127.0.0.1:{port}/", timeout=0.5) == (False, None)  # closed port
