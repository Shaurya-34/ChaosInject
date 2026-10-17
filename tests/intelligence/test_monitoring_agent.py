"""Monitoring agent: severity rules, evidence from records, fixes, reports, optional model rewording."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adapters.fixtures import fixture_ports
from app.api.config import Settings
from app.intelligence import monitoring_agent as agent
from app.intelligence.reasoning import DeterministicProvider, MockProvider
from app.main import create_app
from chaos import hooks

ROOT = Path(__file__).resolve().parents[2]
DEP_MAP = json.loads((ROOT / "docs" / "dependency-map.json").read_text(encoding="utf-8"))


def samples(n, ok, latency, t0=0.0):
    return [{"t": t0 + i, "ok": ok, "status": 200 if ok else 500, "latency_ms": latency} for i in range(n)]


def record(name="r", target="hotel-sandbox-geo-1", fault="container_stop", *, base=40.0, during=None, failed=0,
           total=10, recovered=True, recovery_s=1.0, stop="duration elapsed", refused=False):
    """A run record shaped like chaos.hooks.save_result output (summary computed by the real code)."""
    during_samples = samples(total - failed, True, during if during is not None else base) + samples(failed, False, 3.0)
    rec = {
        "experiment": {"name": name, "target": target, "fault": fault, "duration_s": 20, "probe_url": "http://x",
                       "baseline_samples": 5, "interval_s": 1},
        "policy": {}, "baseline": samples(5, True, base), "during": [] if refused else during_samples,
        "recovery": samples(2, True, base), "stop_reason": ("refused: " if refused else "") + stop,
        "recovered": recovered, "recovery_s": recovery_s, "fault_started": 5.0, "fault_ended": 15.0,
    }
    if refused:
        rec["refused"] = True
    rec["run_id"] = f"20261017-000000-{name}-{abs(hash((name, target, fault))) % 10**6:06d}"
    rec["summary"] = {k: hooks.summarize(rec[k]) for k in ("baseline", "during", "recovery")}
    return rec


def sev(runs):
    return [(f["target"], f["fault"], f["severity"]) for f in agent.analyze(runs, DEP_MAP)["findings"]]


def test_not_recovered_is_severity_one():
    assert sev([record(recovered=False, recovery_s=None, failed=10)]) == [("hotel-sandbox-geo-1", "container_stop", 1)]


def test_full_outage_on_the_core_path_is_severity_two():
    # geo is on /hotels; every probe failed, but the system recovered
    assert sev([record(failed=10)]) == [("hotel-sandbox-geo-1", "container_stop", 2)]


def test_full_outage_off_the_core_path_is_not_a_critical_path_finding():
    assert sev([record(target="hotel-sandbox-user-1", failed=10)]) == [("hotel-sandbox-user-1", "container_stop", 3)]


def test_latency_amplification_is_severity_three():
    runs = [record(target="hotel-sandbox-rate-1", fault="network_delay", base=40.0, during=1500.0)]
    assert sev(runs) == [("hotel-sandbox-rate-1", "network_delay", 3)]
    f = agent.analyze(runs, DEP_MAP)["findings"][0]
    assert "40 ms" in f["explanation"] and "1500 ms" in f["explanation"] and "37.5x" in f["explanation"]


def test_a_small_slowdown_on_a_tiny_baseline_is_not_amplification():
    # 5 ms -> 40 ms is 8x but only 35 ms slower: below the absolute floor, so it is only "degraded"
    runs = [record(target="hotel-sandbox-rate-1", fault="network_delay", base=5.0, during=40.0)]
    assert sev(runs) == [("hotel-sandbox-rate-1", "network_delay", 4)]


def test_degraded_but_tolerated_is_severity_four():
    assert sev([record(failed=2, stop="duration elapsed")]) == [("hotel-sandbox-geo-1", "container_stop", 4)]


def test_a_clean_run_has_no_finding():
    result = agent.analyze([record()], DEP_MAP)
    assert result["findings"] == [] and result["runs_without_findings"] == 1


def test_refused_runs_are_not_evaluated():
    result = agent.analyze([record(refused=True, stop="baseline already unhealthy", recovered=False)], DEP_MAP)
    assert result["findings"] == [] and result["runs_not_evaluated"] == 1


def test_findings_are_ranked_by_severity_then_evidence():
    runs = [
        record("a", failed=2),                                                                   # 4
        record("b", target="hotel-sandbox-rate-1", fault="network_delay", during=1500.0),         # 3
        record("c", target="hotel-sandbox-memcached-rate-1", recovered=False, failed=10),        # 1
        record("d", failed=10),                                                                  # 2 (geo stop)
    ]
    assert [s for _, _, s in sev(runs)] == [1, 2, 3]  # the two geo runs merge into one severity-2 finding


def test_runs_of_one_target_and_fault_merge_and_cite_every_run_id():
    a, b = record("a", failed=2), record("b", failed=10, recovered=False, recovery_s=None)
    findings = agent.analyze([a, b], DEP_MAP)["findings"]
    assert len(findings) == 1
    f = findings[0]
    assert f["severity"] == 1 and set(f["run_ids"]) == {a["run_id"], b["run_id"]}
    assert {e["run_id"] for e in f["evidence"]} == set(f["run_ids"])


def test_every_evidence_number_comes_from_the_record():
    run = record(target="hotel-sandbox-rate-1", fault="network_delay", base=40.0, during=1500.0, total=12, recovery_s=0.5)
    e = agent.analyze([run], DEP_MAP)["findings"][0]["evidence"][0]
    assert e["baseline_p95_ms"] == run["summary"]["baseline"]["p95_ms"] == 40.0
    assert e["during_p95_ms"] == run["summary"]["during"]["p95_ms"] == 1500.0
    assert e["during_probes"] == 12 and e["recovery_s"] == 0.5 and e["run_id"] == run["run_id"]


def test_the_documented_rate_panic_gets_its_concrete_fix():
    f = agent.analyze([record(target="hotel-sandbox-memcached-rate-1", recovered=False, failed=10)], DEP_MAP)["findings"][0]
    assert "services/rate/server.go:114" in f["suggested_fix"] and "MongoDB" in f["suggested_fix"]


def test_fixes_fall_back_by_fault_and_node_type():
    assert "replicas" in agent.suggest_fix("hotel-sandbox-geo-1", "container_stop", DEP_MAP)
    assert "cache-aside" in agent.suggest_fix("hotel-sandbox-memcached-profile-1", "container_stop", DEP_MAP)
    assert "deadlines" in agent.suggest_fix("hotel-sandbox-geo-1", "network_delay", DEP_MAP)
    assert "retry budget" in agent.suggest_fix("hotel-sandbox-geo-1", "packet_loss", DEP_MAP)
    assert agent.suggest_fix("unknown", "weird_fault", DEP_MAP) == agent.DEFAULT_FIX


# --- reports ---------------------------------------------------------------------------------


def test_markdown_report_lists_findings_runs_and_fix():
    result = agent.analyze([record(recovered=False, failed=10, recovery_s=None)], DEP_MAP)
    md = agent.render_markdown(result)
    assert md.startswith("# Chaos findings") and "Not recovered / crashed" in md
    assert result["findings"][0]["run_ids"][0] in md and "Suggested fix" in md and "| run_id |" in md


def test_html_report_escapes_run_data():
    run = record(failed=10, stop="<script>alert(1)</script>")
    html_report = agent.render_html(agent.analyze([run], DEP_MAP))
    assert "<script>alert(1)</script>" not in html_report and "&lt;script&gt;" in html_report


def test_empty_reports_say_so():
    result = agent.analyze([], DEP_MAP)
    assert "No weaknesses found" in agent.render_markdown(result) and "No weaknesses found" in agent.render_html(result)


# --- optional model rewording ----------------------------------------------------------------


def reword(findings, text_for):
    return json.dumps({"explanations": [{"id": f["id"], "text": text_for(f)} for f in findings]})


def test_deterministic_provider_changes_nothing():
    findings = agent.analyze([record(failed=10)], DEP_MAP)["findings"]
    assert agent.enrich_explanations(findings, DeterministicProvider()) is findings


def test_valid_model_text_replaces_only_the_explanation():
    findings = agent.analyze([record(target="hotel-sandbox-rate-1", fault="network_delay", during=1500.0)], DEP_MAP)["findings"]
    text = "A slow rate service made the edge much slower: p95 went from 40 ms to 1500 ms."
    out = agent.enrich_explanations(findings, MockProvider([reword(findings, lambda f: text)]))
    assert out[0]["explanation"] == text and out[0]["explanation_source"] == "mock"
    assert {k: out[0][k] for k in ("severity", "run_ids", "suggested_fix", "evidence")} == \
           {k: findings[0][k] for k in ("severity", "run_ids", "suggested_fix", "evidence")}


@pytest.mark.parametrize("bad", ["It affected 4242 users.", "x" * 500, ""])
def test_invalid_model_text_falls_back(bad):
    findings = agent.analyze([record(failed=10)], DEP_MAP)["findings"]
    assert agent.enrich_explanations(findings, MockProvider([reword(findings, lambda f: bad)] * 2)) == findings


def test_model_outage_falls_back():
    findings = agent.analyze([record(failed=10)], DEP_MAP)["findings"]
    assert agent.enrich_explanations(findings, MockProvider([ConnectionError("down"), "nope"])) == findings


# --- API ---------------------------------------------------------------------------------------

TOKEN = "t" * 40


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "RESULTS_DIR", tmp_path)
    hooks.save_result(record("one", target="hotel-sandbox-memcached-rate-1", recovered=False, recovery_s=None, failed=10))
    hooks.save_result(record("two", target="hotel-sandbox-rate-1", fault="network_delay", during=1500.0))
    app = create_app(Settings(operator_token=TOKEN), fixture_ports())
    with TestClient(app, headers={"Authorization": "Bearer " + TOKEN}) as c:
        yield c


def test_findings_route_ranks_saved_runs(client):
    body = client.get("/api/v1/chaos/findings").json()
    assert [f["severity"] for f in body["findings"]] == [1, 3]
    assert body["runs_analyzed"] == 2 and body["limitations"] == []
    assert all(f["run_ids"] for f in body["findings"])


def test_report_route_returns_markdown_and_html(client):
    md = client.get("/api/v1/chaos/findings/report")
    assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown") and "# Chaos findings" in md.text
    html_report = client.get("/api/v1/chaos/findings/report?format=html")
    assert html_report.headers["content-type"].startswith("text/html") and "<h1>Chaos findings</h1>" in html_report.text
    assert client.get("/api/v1/chaos/findings/report?format=pdf").status_code == 422


def test_findings_routes_need_the_token(client):
    for path in ("/api/v1/chaos/findings", "/api/v1/chaos/findings/report"):
        assert client.get(path, headers={"Authorization": "bad"}).status_code == 401


def test_findings_with_no_runs_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "RESULTS_DIR", tmp_path)
    app = create_app(Settings(operator_token=TOKEN), fixture_ports())
    with TestClient(app, headers={"Authorization": "Bearer " + TOKEN}) as c:
        body = c.get("/api/v1/chaos/findings").json()
    assert body["findings"] == [] and body["runs_analyzed"] == 0
