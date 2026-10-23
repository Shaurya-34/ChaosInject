"""Resilience score: formula, safety-stop cap, aggregation, coverage, traceability, API, reports, docs."""

import json
import random
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.config import Settings
from app.intelligence import resilience_score as rs
from app.intelligence.injection_agent import criticality
from app.main import create_app
from chaos import hooks

ROOT = Path(__file__).resolve().parents[2]
DEP_MAP = json.loads((ROOT / "docs" / "dependency-map.json").read_text(encoding="utf-8"))
POLICY = json.loads((ROOT / "chaos" / "policy.json").read_text(encoding="utf-8"))

GEO, RATE, MEMC = "hotel-sandbox-geo-1", "hotel-sandbox-rate-1", "hotel-sandbox-memcached-rate-1"
MEMC_PROFILE = "hotel-sandbox-memcached-profile-1"
MEMC_RESERVE = "hotel-sandbox-memcached-reserve-1"
_counter = iter(range(1, 10**6))


def samples(n, ok, latency, t0=0.0):
    return [{"t": t0 + i, "ok": ok, "status": 200 if ok else 500, "latency_ms": latency} for i in range(n)]


def record(target=GEO, fault="container_stop", *, base=40.0, during=None, failed=0, total=10, recovered=True,
           recovery_s=1.0, stop="duration elapsed", refused=False, saved_at=None, no_baseline=False):
    """A run record shaped like chaos.hooks.save_result output (the summary comes from the real code)."""
    n = next(_counter)
    during_samples = samples(total - failed, True, during if during is not None else base) + samples(failed, False, 3.0)
    rec = {
        "experiment": {"name": f"e{n}", "target": target, "fault": fault, "duration_s": 20, "probe_url": "http://x",
                       "baseline_samples": 5, "interval_s": 1},
        "policy": {}, "baseline": [] if no_baseline else samples(5, True, base),
        "during": [] if refused else during_samples, "recovery": samples(2, True, base),
        "stop_reason": ("refused: " if refused else "") + stop, "recovered": recovered, "recovery_s": recovery_s,
        "fault_started": 5.0, "fault_ended": 15.0, "run_id": f"run-{n:04d}", "saved_at": saved_at if saved_at is not None else float(n),
    }
    if refused:
        rec["refused"] = True
    rec["summary"] = {k: hooks.summarize(rec[k]) for k in ("baseline", "during", "recovery")}
    return rec


def score(runs):
    return rs.compute(runs, DEP_MAP, POLICY)


# --- run score ---------------------------------------------------------------------------------


def test_geo_stop_is_55_before_the_cap_and_50_after_it():
    # all probes failed (no p95), recovered in 1.2 s: 100 x (0.40 + 0 + 0 + 0.15) = 55
    clean = rs.score_run(record(failed=10, recovery_s=1.2, stop="duration elapsed"))
    assert clean["score"] == 55.0 and not clean["capped"]
    capped = rs.score_run(record(failed=10, recovery_s=1.2, stop="safety: 5 consecutive failures"))
    assert capped["uncapped_score"] == 55.0 and capped["score"] == 50.0
    assert capped["capped"] and capped["cap_reason"] == "safety stop"


def test_rate_delay_is_80_5_and_50_when_the_safety_stop_fired():
    # 0% errors, p95 40 -> 1500 ms (37.5x), recovered in 0.5 s: 100 x (0.40 + 0.25 + 0.20/37.5 + 0.15) = 80.5
    ok = rs.score_run(record(RATE, "network_delay", base=40.0, during=1500.0, recovery_s=0.5))
    assert ok["score"] == 80.5 and not ok["capped"]
    stopped = rs.score_run(record(RATE, "network_delay", base=40.0, during=1500.0, recovery_s=0.5, stop="safety: p95 latency 1500 ms"))
    assert stopped["score"] == 50.0 and stopped["uncapped_score"] == 80.5 and stopped["capped"]


def test_a_run_that_did_not_recover_scores_zero_even_with_no_errors():
    r = rs.score_run(record(recovered=False, recovery_s=None, failed=0))
    assert r["score"] == 0.0 and r["not_recovered"] and not r["capped"]
    assert rs.score_run(record(recovered=False, recovery_s=None, failed=10, stop="safety: x"))["score"] == 0.0


def test_a_perfect_run_scores_100_and_the_cap_does_not_raise_low_scores():
    assert rs.score_run(record())["score"] == 100.0
    low = rs.score_run(record(failed=10, recovery_s=29.0, stop="safety: x"))
    assert low["score"] < 50 and not low["capped"]


def test_other_stops_are_not_capped():
    assert rs.score_run(record(stop="kill switch (operator stop)", recovery_s=0.5))["score"] == 100.0
    assert rs.score_run(record(stop="hard timeout", recovery_s=0.5))["score"] == 100.0


@pytest.mark.parametrize("recovery_s,expected", [(0.0, 1.0), (2.0, 1.0), (16.0, 0.5), (30.0, 0.0), (45.0, 0.0)])
def test_recovery_time_curve_is_full_up_to_2s_and_zero_at_the_policy_timeout(recovery_s, expected):
    c = rs.score_run(record(recovery_s=recovery_s), recovery_timeout_s=30.0)["components"]["recovery_time"]
    assert c["value"] == expected


def test_latency_part_follows_the_inflation_ratio():
    def part(during):
        return rs.score_run(record(base=40.0, during=during))["components"]["latency"]["value"]
    assert part(40.0) == 1.0 and part(20.0) == 1.0 and part(80.0) == 0.5 and part(200.0) == 0.2


def test_missing_baseline_p95_drops_the_latency_part_and_renormalises():
    r = rs.score_run(record(no_baseline=True, failed=0, recovery_s=1.0))
    assert r["components"]["latency"] == {"value": None, "weight": 0.2, "included": False}
    assert r["score"] == 100.0  # the remaining parts are all perfect


def test_recovered_without_a_recovery_time_renormalises_that_part_away():
    r = rs.score_run(record(recovery_s=None))
    assert not r["components"]["recovery_time"]["included"] and r["score"] == 100.0


def test_unscorable_runs_are_named_with_a_reason():
    assert "refused" in rs.unscored_reason(record(refused=True, recovered=False))
    no_during = record()
    no_during["summary"]["during"] = hooks.summarize([])
    assert "no fault-phase probes" in rs.unscored_reason(no_during)
    broken = record()
    del broken["recovered"]
    assert "recovered flag" in rs.unscored_reason(broken)
    assert rs.unscored_reason(record()) is None


# --- aggregation -------------------------------------------------------------------------------


def test_the_latest_run_of_a_pair_counts_and_all_run_ids_stay_listed():
    before = record(MEMC, "container_stop", recovered=False, recovery_s=None, failed=10, saved_at=100.0)
    after = record(MEMC, "container_stop", saved_at=200.0)
    pair = score([after, before])["targets"][0]["pairs"][0]
    assert pair["scored_run_id"] == after["run_id"] and pair["score"] == 100.0
    assert pair["run_ids"] == [before["run_id"], after["run_id"]]  # oldest first
    assert score([before])["targets"][0]["pairs"][0]["score"] == 0.0  # before the fix


def test_a_target_is_half_mean_and_half_worst_pair():
    runs = [record(GEO, "container_stop"), record(GEO, "network_delay", recovered=False, recovery_s=None, failed=10)]
    t = score(runs)["targets"][0]
    assert sorted(p["score"] for p in t["pairs"]) == [0.0, 100.0]
    assert t["score"] == 25.0  # 0.5 x mean(100, 0) + 0.5 x min(100, 0)


def test_overall_is_weighted_by_criticality_with_a_floor():
    runs = [record(GEO), record(RATE, "container_stop", recovered=False, recovery_s=None, failed=10)]
    out = score(runs)
    weights = {t["target"]: t["weight"] for t in out["targets"]}
    assert weights[GEO] == max(0.1, criticality(DEP_MAP, "geo")) == weights[RATE]
    assert out["score"] == 50.0  # equal weights: mean(100, 0)
    assert all(t["criticality"] == criticality(DEP_MAP, t["node"]) for t in out["targets"])


def test_unmapped_targets_are_listed_but_left_out_of_the_overall_score():
    dummy = record("chaos-dummy-web", "container_stop", recovered=False, recovery_s=None, failed=10)
    out = score([record(GEO), dummy])
    assert out["score"] == 100.0
    assert [t["target"] for t in out["unmapped_targets"]] == ["chaos-dummy-web"] and out["unmapped_targets"][0]["score"] == 0.0


def test_no_scored_runs_means_null_not_zero():
    for runs in ([], [record(refused=True, recovered=False)]):
        out = score(runs)
        assert out["score"] is None and out["headline"].startswith("n/a") and out["coverage"]["tested"] == 0
    assert len(score([record(refused=True, recovered=False)])["runs_not_scored"]) == 1


def test_coverage_counts_tested_pairs_of_the_possible_ones():
    out = score([record(GEO, "container_stop"), record(RATE, "network_delay")])
    cov = out["coverage"]
    assert (cov["tested"], cov["possible"]) == (2, 15)  # 5 mapped allowed targets x 3 faults
    assert len(cov["untested"]) == 13 and not out["complete"]
    assert out["headline"] == "100 (partial: 2 of 15 pairs tested)"


def test_full_coverage_gives_a_plain_headline():
    runs = [record(t, f) for t in (GEO, RATE, MEMC, MEMC_PROFILE, MEMC_RESERVE) for f in ("container_stop", "network_delay", "packet_loss")]
    out = score(runs)
    assert out["complete"] and out["headline"] == "100" and out["coverage"]["untested"] == []


def test_the_dummy_target_does_not_count_toward_coverage():
    assert score([record("chaos-dummy-web", "container_stop")])["coverage"]["tested"] == 0


def test_the_result_does_not_depend_on_run_order():
    runs = [record(GEO, "container_stop", failed=3), record(RATE, "network_delay", during=900.0),
            record(MEMC, "packet_loss", recovered=False, recovery_s=None, failed=10), record(GEO, "network_delay", saved_at=500.0)]
    shuffled = list(runs)
    random.Random(7).shuffle(shuffled)
    assert score(runs) == score(shuffled) == score(list(reversed(runs)))


def test_every_number_traces_back_to_a_run_record():
    runs = [record(GEO, failed=10, recovery_s=1.2, stop="safety: x"), record(RATE, "network_delay", base=40.0, during=1500.0, recovery_s=0.5)]
    by_id = {r["run_id"]: r for r in runs}
    for target in score(runs)["targets"]:
        for pair in target["pairs"]:
            rec = by_id[pair["scored_run_id"]]
            assert pair["scored_run_id"] in pair["run_ids"]
            assert pair["inputs"]["error_rate"] == rec["summary"]["during"]["error_rate"]
            assert pair["inputs"]["baseline_p95_ms"] == rec["summary"]["baseline"]["p95_ms"]
            assert pair["inputs"]["during_p95_ms"] == rec["summary"]["during"]["p95_ms"]
            assert pair["inputs"]["recovery_s"] == rec["recovery_s"] and pair["inputs"]["stop_reason"] == rec["stop_reason"]
            assert pair["inputs"]["recovery_timeout_s"] == POLICY["recovery_timeout_s"]


def test_weights_sum_to_one():
    assert round(sum(rs.WEIGHTS.values()), 10) == 1.0


# --- docs --------------------------------------------------------------------------------------


def test_the_doc_states_the_weights_the_cap_and_the_not_recovered_rule():
    doc = (ROOT / "docs" / "resilience-score.md").read_text(encoding="utf-8")
    for needle in ("0.40", "0.25", "0.20", "0.15", "safety:", "50", "not recovered", "recovery_s", "recovery_timeout_s", "run_id"):
        assert needle.lower() in doc.lower(), needle
    assert "55" in doc and "80.5" in doc


# --- API and reports ---------------------------------------------------------------------------

TOKEN = "t" * 40


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "RESULTS_DIR", tmp_path)
    app = create_app(Settings(operator_token=TOKEN))
    with TestClient(app, headers={"Authorization": "Bearer " + TOKEN}) as c:
        yield c


def save(**kw):
    return hooks.save_result({k: v for k, v in record(**kw).items() if k not in ("run_id", "saved_at", "summary")})


def test_score_route_is_null_with_no_runs(client):
    body = client.get("/api/v1/chaos/score").json()
    assert body["score"] is None and body["headline"].startswith("n/a") and body["coverage"]["possible"] == 15


def test_score_route_and_findings_carry_the_same_score(client):
    save(target=GEO, failed=10, recovery_s=1.2, stop="safety: 5 consecutive failures")
    save(target=RATE, fault="network_delay", base=40.0, during=1500.0, recovery_s=0.5)
    score_body = client.get("/api/v1/chaos/score").json()
    assert score_body["score"] == round((50.0 + 80.5) / 2, 1)  # geo capped to 50, rate delay 80.5, equal criticality weights
    assert client.get("/api/v1/chaos/findings").json()["score"] == score_body


def test_the_markdown_and_html_reports_include_the_score_breakdown(client):
    save(target=GEO, failed=10, recovery_s=1.2, stop="safety: 5 consecutive failures")
    md = client.get("/api/v1/chaos/findings/report").text
    assert "## Resilience score" in md and "capped from 55: safety stop" in md and "scored run" in md
    html_report = client.get("/api/v1/chaos/findings/report?format=html").text
    assert "Resilience score:" in html_report and "capped from 55" in html_report


def test_score_routes_need_the_token(client):
    assert client.get("/api/v1/chaos/score", headers={"Authorization": "bad"}).status_code == 401
