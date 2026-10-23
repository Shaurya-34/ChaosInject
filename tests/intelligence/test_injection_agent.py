"""Injection agent: ranking, policy validation, read-only behaviour, optional model rewording."""

import ast
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import chaos_runs
from app.api.config import Settings
from app.intelligence import injection_agent as agent
from app.intelligence.reasoning import DeterministicProvider, MockProvider
from app.main import create_app
from chaos import controller, hooks

ROOT = Path(__file__).resolve().parents[2]
DEP_MAP = json.loads((ROOT / "docs" / "dependency-map.json").read_text(encoding="utf-8"))
POLICY = json.loads((ROOT / "chaos" / "policy.json").read_text(encoding="utf-8"))


def top(runs=(), **kw):
    return agent.propose(DEP_MAP, list(runs), POLICY, **kw)


def key(p):
    return p["experiment"]["target"], p["experiment"]["fault"]


def test_every_proposal_passes_the_controller_policy():
    result = top()
    assert result["proposals"]
    for p in result["proposals"]:
        controller.check_policy(p["experiment"], POLICY)  # must not raise
        assert p["experiment"]["target"] in POLICY["allowed_targets"]
        assert p["rationale"] and p["expected_impact"] and p["score"] > 0


def test_proposals_look_like_experiment_files():
    exp = top()["proposals"][0]["experiment"]
    for field in ("name", "target", "fault", "duration_s", "probe_url", "baseline_samples", "interval_s"):
        assert field in exp
    assert exp["duration_s"] <= POLICY["max_duration_s"]


def test_policy_refusals_are_reported_with_a_reason():
    rejected = top()["rejected"]
    frontend = [r for r in rejected if r["target"] == "hotel-sandbox-frontend-1"]
    assert len(frontend) == len(agent.FAULT_TEMPLATES)
    assert all("not allowlisted" in r["reason"] for r in frontend)


def test_faults_not_in_the_policy_are_dropped():
    policy = {**POLICY, "allowed_faults": ["container_stop"]}
    result = agent.propose(DEP_MAP, [], policy, config=agent.AgentConfig(top_n=50))
    assert {p["experiment"]["fault"] for p in result["proposals"]} == {"container_stop"}
    assert any("fault 'packet_loss' is not allowlisted" in r["reason"] for r in result["rejected"])


def test_impact_follows_transitive_dependencies():
    # memcached-rate is read by rate, rate is called by search, search is called by frontend on /hotels.
    assert "/hotels" in agent.affected_endpoints(DEP_MAP, "memcached-rate")
    assert "rate" in agent.dependents(DEP_MAP, "memcached-rate")
    assert "search" in agent.dependents(DEP_MAP, "rate")


def test_endpoints_with_broken_discovery_are_not_counted():
    # review and attractions are marked broken in the shipped configuration.
    assert agent.affected_endpoints(DEP_MAP, "review") == []
    assert agent.criticality(DEP_MAP, "review") == 0.0


def test_a_backend_failure_is_not_blamed_for_unrelated_endpoints():
    hit = agent.affected_endpoints(DEP_MAP, "rate")
    assert "/hotels" in hit and "/user" not in hit and "/reservation" not in hit
    assert set(agent.affected_endpoints(DEP_MAP, "frontend")) == {"/hotels", "/recommendations", "/user", "/reservation"}


def test_criticality_separates_core_path_from_peripheral_services():
    assert agent.criticality(DEP_MAP, "frontend") == 1.0
    assert agent.criticality(DEP_MAP, "rate") > agent.criticality(DEP_MAP, "user") > 0
    assert agent.criticality(DEP_MAP, "consul") == 0.0


def test_untested_experiments_rank_above_ones_already_run():
    first = top(config=agent.AgentConfig(top_n=50))["proposals"]
    best = first[0]
    target, fault = key(best)
    history = [{"target": target, "fault": fault, "recovered": True}] * 3
    after = top(history, config=agent.AgentConfig(top_n=50))["proposals"]
    again = next(p for p in after if key(p) == (target, fault))
    assert again["times_run"] == 3 and again["score"] < best["score"]
    assert after.index(again) > 0


def test_a_previous_failure_raises_the_target():
    base = {key(p): p["score"] for p in top(config=agent.AgentConfig(top_n=50))["proposals"]}
    failed = [{"target": "hotel-sandbox-geo-1", "fault": "container_stop", "recovered": False}]
    after = {key(p): p["score"] for p in top(failed, config=agent.AgentConfig(top_n=50))["proposals"]}
    # geo network_delay was never run, but geo failed to recover before: its score goes up.
    assert after[("hotel-sandbox-geo-1", "network_delay")] > base[("hotel-sandbox-geo-1", "network_delay")]


def test_names_do_not_collide_with_existing_experiments():
    result = top(existing_experiments={"hotel-geo-stop", "hotel-rate-delay", "hotel-rate-loss"})
    names = {p["experiment"]["name"] for p in agent.propose(DEP_MAP, [], POLICY, {"hotel-geo-stop"}, agent.AgentConfig(top_n=50))["proposals"]}
    assert "hotel-geo-stop" not in names and "hotel-geo-stop-2" in names
    assert len(result["proposals"]) <= agent.AgentConfig().top_n


def test_proposing_is_pure_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = sorted(p.name for p in (ROOT / "experiments").iterdir())
    top()
    assert sorted(p.name for p in (ROOT / "experiments").iterdir()) == before
    assert list(tmp_path.iterdir()) == []


def test_intelligence_package_never_imports_a_process_or_docker_api():
    banned = {"subprocess", "docker", "shlex", "pty"}
    for path in (ROOT / "backend" / "app" / "intelligence").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            assert not banned & set(names), f"{path.name} imports {banned & set(names)}"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert not (node.func.attr in {"system", "popen"} and getattr(node.func.value, "id", "") == "os"), path.name


# --- optional model rewording --------------------------------------------------------------


def test_deterministic_provider_leaves_proposals_unchanged():
    proposals = top()["proposals"]
    assert agent.enrich_rationales(proposals, DeterministicProvider()) is proposals


def reword(proposals, text_for):
    return json.dumps({"rationales": [{"id": p["experiment"]["name"], "text": text_for(p)} for p in proposals]})


def test_valid_model_text_replaces_the_rationale_only():
    proposals = top(config=agent.AgentConfig(top_n=3))["proposals"]
    provider = MockProvider([reword(proposals, lambda p: f"Worth testing {p['experiment']['fault']} on {p['experiment']['target']}.")])
    out = agent.enrich_rationales(proposals, provider)
    assert [key(p) for p in out] == [key(p) for p in proposals]
    assert all(o["rationale_source"] == "mock" and o["rationale"].startswith("Worth testing") for o in out)
    assert [o["score"] for o in out] == [p["score"] for p in proposals]


@pytest.mark.parametrize("bad", [
    lambda p: "It will cause 999 failures.",  # number not in the facts
    lambda p: "x" * 400,                      # too long
    lambda p: "",                             # empty
])
def test_invalid_model_text_falls_back(bad):
    proposals = top(config=agent.AgentConfig(top_n=2))["proposals"]
    out = agent.enrich_rationales(proposals, MockProvider([reword(proposals, bad)] * 2))
    assert out == proposals


def test_model_outage_and_garbage_fall_back():
    proposals = top(config=agent.AgentConfig(top_n=2))["proposals"]
    assert agent.enrich_rationales(proposals, MockProvider([ConnectionError("down"), "not json"])) == proposals


# --- API --------------------------------------------------------------------------------------

TOKEN = "t" * 40


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "RESULTS_DIR", tmp_path)
    app = create_app(Settings(operator_token=TOKEN))
    with TestClient(app, headers={"Authorization": "Bearer " + TOKEN}) as c:
        yield c


def test_proposals_route_returns_ranked_valid_proposals(client):
    r = client.get("/api/v1/chaos/proposals")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposals"] and body["rejected"] and body["limitations"] == []
    scores = [p["score"] for p in body["proposals"]]
    assert scores == sorted(scores, reverse=True)
    for p in body["proposals"]:
        controller.check_policy(p["experiment"], POLICY)


def test_proposals_route_requires_the_token(client):
    assert client.get("/api/v1/chaos/proposals", headers={"Authorization": "bad"}).status_code == 401


def test_proposals_route_does_not_depend_on_execution_being_enabled(client, monkeypatch):
    spawned = []
    monkeypatch.setattr(chaos_runs, "_spawn", lambda path: spawned.append(path))
    assert client.get("/api/v1/chaos/proposals").status_code == 200
    assert spawned == []
