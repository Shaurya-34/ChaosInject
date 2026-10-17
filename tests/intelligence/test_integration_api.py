"""The intelligence service plugged into Sachin's real FastAPI app (fixture ports otherwise)."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.adapters.fixtures import fixture_ports
from app.api.config import Settings
from app.contracts import QueryDecision
from app.intelligence import IntelligenceService, fakes
from app.intelligence.learner import StreamingLearner
from app.intelligence.rollback import RollbackDisabled, propose_rollback
from app.main import create_app

from .helpers import Req, run

TOKEN = "t" * 40


@pytest.fixture
def client(allow):
    ports = fixture_ports(True, delay=0.05)
    ports.intelligence = IntelligenceService(allow, learner=StreamingLearner(source="fixture"))
    ports.estimator = fakes.FakePlanEstimator(allow)
    with TestClient(create_app(Settings(operator_token=TOKEN, fake_runtime_enabled=True), ports), headers={"Authorization": "Bearer " + TOKEN}) as c:
        yield c


def test_sql_analyze_route(client):
    allow_body = client.post("/api/v1/sql/analyze", json={"sql": "SELECT id, name FROM lab.products WHERE id = $1", "parameters": {"1": 42}})
    assert allow_body.status_code == 200, allow_body.text
    assert allow_body.json()["decision"] == "ALLOW" and allow_body.json()["executed"] is False
    block = client.post("/api/v1/sql/analyze", json={"sql": "SELECT * FROM lab.products"}).json()
    assert block["decision"] == "BLOCK" and "OUT-003" in block["rule_ids"] and block["intent_id"] is None
    write = client.post("/api/v1/sql/analyze", json={"sql": "DELETE FROM lab.products"}).json()
    assert write["decision"] == "BLOCK" and write["rule_ids"] == ["SQL-004"]


def test_plan_route_returns_contract_proposals(client):
    r = client.post("/api/v1/experiments/plan")
    assert r.status_code == 200, r.text
    # Sachin's fixture topology/state/catalog: EXP-00 available, stateless targets only.
    for p in r.json():
        assert p["catalog_id"] == "EXP-00" and p["target_id"] != "redis-cart"


def test_analysis_route_passes_evidence_check(client):
    r = client.get("/api/v1/runs/fixture-run/analysis")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "deterministic_fallback" and body["source"] == "fixture"
    assert body["hypotheses"] and all(h["evidence_refs"] for h in body["hypotheses"])


def test_learning_status_route(client):
    r = client.get("/api/v1/learning/status")
    assert r.status_code == 200 and r.json()["retained_raw_examples"] == 0 and r.json()["persistence"] == "disabled"


def test_rollback_proposals_are_disabled_on_day_one(monkeypatch):
    monkeypatch.delenv("ADAPTIVE_ROLLBACK_ENABLED", raising=False)
    with pytest.raises(RollbackDisabled):
        propose_rollback(sandbox_id="s", target_id="t", prior_replicas=1, applied_replicas=2, evidence_refs=[], observation_seconds=60, auto_revert_requested=False)


def test_decision_cannot_claim_execution(svc, estimator):
    d = run(svc.analyze_query(Req("SELECT id FROM lab.products WHERE id = $1", {"1": 1}), fakes.state(), estimator))
    with pytest.raises(ValidationError):
        QueryDecision(**{**d.model_dump(), "executed": True})


def test_revalidation_has_no_learning_side_effects_and_sees_fresh_state(svc, estimator):
    req = Req("SELECT id FROM lab.products WHERE id = $1", {"1": 1})
    d = run(svc.revalidate(req, fakes.state(), estimator, request_id="req_fixed"))
    assert d.request_id == "req_fixed" and d.decision == "ALLOW"
    c = svc.learner.diagnostics()["counters"]
    assert c["arrivals"] == 0 and c["decisions_allow"] == 0
    assert run(svc.revalidate(req, fakes.state(active_fault_id="run_9"), estimator, "req_x")).decision == "BLOCK"
