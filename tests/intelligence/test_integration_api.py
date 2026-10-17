"""The intelligence service plugged into Sachin's real FastAPI app (fixture ports otherwise)."""

import pytest
from fastapi.testclient import TestClient

from app.adapters.fixtures import fixture_ports
from app.api.config import Settings
from app.intelligence import IntelligenceService
from app.intelligence.rollback import RollbackDisabled, propose_rollback
from app.main import create_app

TOKEN = "t" * 40


@pytest.fixture
def client():
    ports = fixture_ports(True, delay=0.05)
    ports.intelligence = IntelligenceService()
    with TestClient(create_app(Settings(operator_token=TOKEN, fake_runtime_enabled=True), ports), headers={"Authorization": "Bearer " + TOKEN}) as c:
        yield c


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


def test_rollback_proposals_are_disabled_on_day_one(monkeypatch):
    monkeypatch.delenv("ADAPTIVE_ROLLBACK_ENABLED", raising=False)
    with pytest.raises(RollbackDisabled):
        propose_rollback(sandbox_id="s", target_id="t", prior_replicas=1, applied_replicas=2, evidence_refs=[], observation_seconds=60, auto_revert_requested=False)

