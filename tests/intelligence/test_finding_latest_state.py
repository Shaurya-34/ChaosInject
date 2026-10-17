"""Findings say what the LATEST run showed: open, improved since, or fixed since. Deterministic; the score agrees."""

import json
import random
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters.fixtures import fixture_ports
from app.api.config import Settings
from app.intelligence import monitoring_agent as agent, resilience_score
from app.intelligence.reasoning import LiveProvider
from app.intelligence import llm_transport as lt
from app.main import create_app
from chaos import hooks

from .test_monitoring_agent import DEP_MAP, record

ROOT = Path(__file__).resolve().parents[2]
POLICY = json.loads((ROOT / "chaos" / "policy.json").read_text(encoding="utf-8"))
MEMC = "hotel-sandbox-memcached-rate-1"
_t = iter(range(1000, 10**6))


def run(saved_at=None, **kw):
    """A record with an explicit, increasing saved_at so 'latest' is unambiguous."""
    rec = record(**kw)
    n = next(_t)
    rec["saved_at"] = float(n) if saved_at is None else float(saved_at)
    rec["run_id"] = f"{rec['run_id']}-{n}"  # real run ids are unique; the shared helper's are not
    return rec


def bad(**kw):
    return run(target=kw.pop("target", MEMC), recovered=False, recovery_s=None, failed=10, **kw)


def fixed(**kw):
    return run(target=kw.pop("target", MEMC), **kw)


def finding(runs):
    out = agent.analyze(runs, DEP_MAP)["findings"]
    assert len(out) == 1
    return out[0]


def test_shaurya_scenario_two_failed_runs_then_a_fixed_one_reads_as_fixed_since():
    runs = [bad(), bad(), fixed()]
    f = finding(runs)
    assert f["status"] == "fixed_since" and f["status_label"] == "Fixed since"
    assert f["severity"] == 1 and f["severity_label"] == "Not recovered / crashed"  # severity logic is untouched
    assert f["explanation"].startswith("Fixed since: the latest run showed no weakness")
    assert "2 of the 2 earlier run(s)" in f["explanation"]
    assert f["latest_run_id"] == runs[2]["run_id"] and f["latest_severity"] is None and f["latest_state"] == "No weakness"
    assert f["runs_total"] == 3 and f["runs_with_weakness"] == 2
    assert f["run_ids"] == [r["run_id"] for r in runs]  # all three, oldest first
    assert [e["weakness_severity"] for e in f["evidence"]] == [1, 1, None]


def test_the_latest_run_decides_not_the_input_order():
    runs = [bad(), bad(), fixed()]
    for order in (list(reversed(runs)), random.Random(3).sample(runs, 3)):
        f = finding(order)
        assert f["status"] == "fixed_since" and f["latest_run_id"] == runs[2]["run_id"]
        assert f["run_ids"] == [r["run_id"] for r in runs]


def test_a_milder_latest_run_is_improved_since():
    runs = [bad(), run(target=MEMC, failed=2, stop="duration elapsed")]  # newest: degraded but recovered (severity 4)
    f = finding(runs)
    assert f["status"] == "improved" and f["severity"] == 1 and f["latest_severity"] == 4
    assert f["explanation"].startswith("Improved since: the latest run is milder")
    assert "1 of 2 runs showed a weakness" not in f["explanation"] and "2 of 2 runs showed a weakness" in f["explanation"]


def test_a_latest_run_as_bad_as_the_worst_stays_open():
    f = finding([bad(), bad(), bad()])
    assert f["status"] == "open" and f["latest_severity"] == 1
    assert f["explanation"] == "In 3 run(s), the system did not recover after the fault was removed."  # the wording is unchanged


def test_open_with_an_older_clean_run_mentions_the_latest_run():
    f = finding([fixed(), bad(), bad()])
    assert f["status"] == "open" and f["runs_total"] == 3 and f["runs_with_weakness"] == 2
    assert f["explanation"].startswith("Latest run: the system did not recover")
    assert "2 of 3 runs showed a weakness" in f["explanation"]


def test_a_regression_after_a_fix_is_open_again():
    f = finding([bad(), fixed(), bad()])
    assert f["status"] == "open" and f["latest_severity"] == 1


def test_a_pair_that_was_only_ever_clean_has_no_finding():
    result = agent.analyze([fixed(), fixed()], DEP_MAP)
    assert result["findings"] == [] and result["runs_without_findings"] == 2


def test_a_refused_newest_run_is_not_treated_as_the_latest_state():
    refused = run(target=MEMC, refused=True, recovered=False, stop="baseline already unhealthy")
    result = agent.analyze([bad(), refused], DEP_MAP)
    f = result["findings"][0]
    assert f["status"] == "open" and f["runs_total"] == 1 and refused["run_id"] not in f["run_ids"]
    assert result["runs_not_evaluated"] == 1


def test_findings_that_are_fixed_since_rank_after_open_ones_even_if_more_severe():
    old_fixed = [bad(target="hotel-sandbox-geo-1"), fixed(target="hotel-sandbox-geo-1")]  # severity 1, fixed
    still_open = [run(target="hotel-sandbox-rate-1", fault="network_delay", base=40.0, during=1500.0)]  # severity 3, open
    out = agent.analyze(old_fixed + still_open, DEP_MAP)["findings"]
    assert [(f["target"], f["status"]) for f in out] == [("hotel-sandbox-rate-1", "open"), ("hotel-sandbox-geo-1", "fixed_since")]
    assert [f["severity"] for f in out] == [3, 1]


def test_runs_without_a_saved_at_are_ordered_deterministically_by_run_id():
    a, b = record(failed=10, recovered=False, recovery_s=None), record()
    a["run_id"], b["run_id"] = "run-a", "run-b"  # b sorts last, so b is the latest
    assert finding([a, b])["status"] == "fixed_since"
    assert finding([b, a])["status"] == "fixed_since"


def test_the_score_and_the_finding_agree_on_which_run_is_the_latest():
    runs = [bad(), bad(), fixed()]
    f = finding(runs)
    score = resilience_score.compute(runs, DEP_MAP, POLICY)
    pair = score["targets"][0]["pairs"][0]
    assert pair["scored_run_id"] == f["latest_run_id"] and pair["score"] == 100.0


def test_reports_show_the_status():
    result = agent.analyze([bad(), bad(), fixed()], DEP_MAP)
    md = agent.render_markdown(result)
    assert "**Status: Fixed since** (latest run: no weakness)" in md
    html_report = agent.render_html(result)
    assert "Status: Fixed since" in html_report and "latest run: no weakness" in html_report
    open_md = agent.render_markdown(agent.analyze([bad()], DEP_MAP))
    assert "**Status: Open**" in open_md and "(latest run" not in open_md


def test_the_model_guard_still_checks_the_new_wording():
    f = agent.analyze([bad(), bad(), fixed()], DEP_MAP)["findings"]

    def provider(text):
        def handler(request):
            data = json.loads(json.loads(request.content)["messages"][1]["content"].split("DATA:\n", 1)[1])
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"explanations": [{"id": d["id"], "text": text} for d in data]})}}]})
        transport = lt.make_transport("https://api.example.com/v1", "m", "k", http_transport=httpx.MockTransport(handler))
        return LiveProvider(transport=transport, model_id="m")

    ok = agent.enrich_explanations(f, provider("Fixed since: the latest run was fine, but 2 earlier runs did not recover."))
    assert ok[0]["explanation_source"] == "live" and ok[0]["status"] == "fixed_since"
    assert agent.enrich_explanations(f, provider("Fixed since, and 9999 users were helped.")) == f


# --- API -----------------------------------------------------------------------------------------

TOKEN = "t" * 40


def test_the_findings_route_carries_the_status(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "RESULTS_DIR", tmp_path)
    for kw in ({"recovered": False, "recovery_s": None, "failed": 10}, {"recovered": False, "recovery_s": None, "failed": 10}, {}):
        hooks.save_result({k: v for k, v in record(target=MEMC, **kw).items() if k not in ("run_id", "saved_at", "summary")})
        time.sleep(0.02)
    with TestClient(create_app(Settings(operator_token=TOKEN), fixture_ports()), headers={"Authorization": "Bearer " + TOKEN}) as c:
        body = c.get("/api/v1/chaos/findings").json()
        report = c.get("/api/v1/chaos/findings/report").text
    f = body["findings"][0]
    assert f["status"] == "fixed_since" and f["runs_total"] == 3 and f["latest_run_id"] == f["run_ids"][-1]
    assert "**Status: Fixed since**" in report
