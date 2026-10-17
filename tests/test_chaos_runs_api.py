"""/api/v1/chaos: wraps chaos.controller and chaos.hooks. No Docker: the controller spawn is faked."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adapters.fixtures import fixture_ports
from app.api import chaos_runs
from app.api.config import Settings
from app.main import create_app
from chaos import controller, hooks

TOKEN = "chaos-api-test-token-00000000000000000"
SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "contracts" / "run.schema.json").read_text())


class FakeProc:
    pid = 4242

    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode


@pytest.fixture
def results(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "RESULTS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def spawned(monkeypatch):
    procs = []

    def fake_spawn(path):
        procs.append(FakeProc())
        return procs[-1]

    monkeypatch.setattr(chaos_runs, "_spawn", fake_spawn)
    return procs


def client(enabled):
    app = create_app(Settings(operator_token=TOKEN, chaos_execution_enabled=enabled), fixture_ports())
    return TestClient(app, headers={"Authorization": "Bearer " + TOKEN})


def saved_run(results):
    record = {"experiment": {"name": "hotel-geo-stop", "target": "hotel-sandbox-geo-1", "fault": "container_stop",
                             "duration_s": 20, "probe_url": "http://x", "baseline_samples": 1, "interval_s": 1},
              "policy": {}, "baseline": [{"t": 1.0, "ok": True, "status": 200, "latency_ms": 5.0}],
              "during": [{"t": 2.0, "ok": False, "status": 500, "latency_ms": 9.0}], "recovery": [],
              "stop_reason": "safety: x", "recovered": False, "recovery_s": None,
              "fault_started": 1.5, "fault_ended": 2.5}
    return Path(hooks.save_result(record)).stem


def test_requires_operator_token(results):
    with client(False) as c:
        assert c.get("/api/v1/chaos/runs", headers={"Authorization": "bad"}).status_code == 401


def test_lists_experiments(results):
    with client(False) as c:
        ids = {e["id"]: e for e in c.get("/api/v1/chaos/experiments").json()}
    assert ids["hotel_geo_stop"]["target"] == "hotel-sandbox-geo-1"


def test_runs_list_and_detail_match_contract(results):
    run_id = saved_run(results)
    with client(False) as c:
        rows = c.get("/api/v1/chaos/runs").json()
        detail = c.get(f"/api/v1/chaos/runs/{run_id}").json()
    assert rows[0]["run_id"] == run_id
    assert set(SCHEMA["$defs"]["IndexEntry"]["required"]) <= set(rows[0])
    assert set(SCHEMA["required"]) <= set(detail)
    assert set(SCHEMA["$defs"]["PhaseSummary"]["required"]) <= set(detail["summary"]["during"])


def test_run_detail_unknown_and_bad_ids(results):
    with client(False) as c:
        assert c.get("/api/v1/chaos/runs/nope").status_code == 404
        assert c.get("/api/v1/chaos/runs/.active_fault").status_code == 422
        assert c.get("/api/v1/chaos/runs/a%20b").status_code == 422


def test_execution_disabled_by_default(results, spawned):
    with client(False) as c:
        r = c.post("/api/v1/chaos/experiments/hotel_geo_stop/run")
    assert r.status_code == 409 and r.json()["code"] == "MUTATIONS_DISABLED"
    assert spawned == []


def test_start_status_stop_and_finish(results, spawned):
    with client(True) as c:
        r = c.post("/api/v1/chaos/experiments/hotel_geo_stop/run")
        assert r.status_code == 202 and r.json()["pid"] == 4242
        assert c.post("/api/v1/chaos/experiments/hotel_geo_stop/run").status_code == 409  # one at a time
        assert c.get("/api/v1/chaos/active").json()["running"] is True

        assert c.post("/api/v1/chaos/active/stop").status_code == 202
        assert controller.stop_path().exists()
        assert c.get("/api/v1/chaos/active").json()["stop_requested"] is True

        (results / ".active_run.log").write_text("restored x\nsaved: results/20261017-1-x-abc123.json\n")
        spawned[0].returncode = 0
        a = c.get("/api/v1/chaos/active").json()
    assert (a["running"], a["exit_code"], a["run_id"]) == (False, 0, "20261017-1-x-abc123")
    assert len(spawned) == 1


def test_stop_without_active_run_conflicts(results, spawned):
    with client(True) as c:
        assert c.post("/api/v1/chaos/active/stop").status_code == 409


def test_leftover_fault_marker_blocks_new_run(results, spawned):
    controller.marker_path().write_text(json.dumps({"target": "hotel-sandbox-geo-1", "fault": "container_stop"}))
    with client(True) as c:
        assert c.post("/api/v1/chaos/experiments/hotel_geo_stop/run").status_code == 409
        assert c.get("/api/v1/chaos/active").json()["fault_active"] is True
    assert spawned == []


def test_policy_refused_and_unknown_experiment(results, spawned, tmp_path, monkeypatch):
    exp_dir = tmp_path / "experiments"
    exp_dir.mkdir()
    (exp_dir / "evil.json").write_text(json.dumps({"name": "evil", "target": "postgres", "fault": "container_stop",
                                                   "duration_s": 5}))
    monkeypatch.setattr(chaos_runs, "EXPERIMENTS_DIR", exp_dir)
    with client(True) as c:
        r = c.post("/api/v1/chaos/experiments/evil/run")
        assert r.status_code == 422 and r.json()["code"] == "POLICY_REFUSED"
        assert c.post("/api/v1/chaos/experiments/missing/run").status_code == 404
    assert spawned == []
