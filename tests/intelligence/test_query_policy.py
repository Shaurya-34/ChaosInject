import dataclasses
import logging

import pytest

from app.contracts import Prediction
from app.intelligence import fakes
from app.intelligence.learner import LEARNER_VERSION, StreamingLearner
from app.intelligence.policy import POLICY_VERSION, RULES, QueryPolicyConfig
from app.intelligence.service import IntelligenceService

from .helpers import Req, run

SELECTIVE = ("SELECT id, name, price_cents FROM lab.products WHERE id = $1", {"1": 42})
BROAD = ("SELECT * FROM lab.products", {})


def learner_dump(learner) -> str:
    """Every growable structure the learner holds, rendered as text."""

    lr = learner._model.steps["LinearRegression"]
    parts = [learner._pending, list(learner._queue), learner._consumed, learner.counters, learner._coverage, dict(lr.weights)]
    parts += [(k, m.mean, m.var, m.n) for k, m in learner._moments.items()]
    return repr(parts)


def analyze(svc, sql, params=None, state="healthy", estimator=None):
    st = fakes.state() if state == "healthy" else state
    est = estimator if estimator is not None else fakes.FakePlanEstimator(svc.allowlist)
    return run(svc.analyze_query(Req(sql, params), st, est))


def test_selective_bounded_query_is_allowed(svc):
    d = analyze(svc, *SELECTIVE)
    assert d.decision == "ALLOW" and d.rule_ids == []
    assert d.executed is False and d.policy_version == POLICY_VERSION and d.source == "fixture"
    assert d.plan_estimate.plan_source == "fixture"
    assert any("not a probability" in r for r in d.reasons)


def test_unbounded_broad_query_is_blocked_with_actual_row_count(svc):
    d = analyze(svc, *BROAD)
    assert d.decision == "BLOCK" and "OUT-003" in d.rule_ids
    assert d.plan_estimate.estimated_rows == fakes.LAB_TABLE_ROWS["lab.products"]
    assert any("10000 rows" in r for r in d.reasons)
    joined = " ".join(d.recommendations)
    for template in ("REC-KEYSET-PAGINATION", "REC-PROJECT-COLUMNS", "REC-BOUNDED-EXPORT"):
        assert template in joined
    keyset = next(r for r in d.recommendations if r.startswith("REC-KEYSET-PAGINATION"))
    assert "'id'" in keyset and "changes the result set" in keyset  # built from the allowlisted primary key


def test_count_aggregate_is_not_treated_like_select_star(svc):
    d = analyze(svc, "SELECT count(*) FROM lab.stock_levels")
    assert d.decision == "ALLOW" and d.plan_estimate.estimated_rows == 1


def test_limit_with_unindexed_sort_warns(svc):
    d = analyze(svc, "SELECT id, name FROM lab.products ORDER BY name LIMIT 10")
    assert d.decision == "WARN" and d.rule_ids == ["SORT-001"]
    assert any("LIMIT alone does not make an explicit sort cheap" in r for r in d.recommendations)


def test_limit_on_indexed_order_is_allowed(svc):
    assert analyze(svc, "SELECT id, name FROM lab.products ORDER BY id LIMIT 10").decision == "ALLOW"


def test_limit_above_interactive_budget_warns_about_truncation(svc):
    d = analyze(svc, "SELECT id, name FROM lab.products LIMIT 5000")
    assert d.decision == "WARN" and "OUT-004" in d.rule_ids


def test_hard_limits_block_even_with_limit(svc, allow):
    big = fakes.FakePlanEstimator(allow, {"lab.products": 2_000_000})
    d = analyze(svc, "SELECT * FROM lab.products LIMIT 1000000", estimator=big)
    assert d.decision == "BLOCK" and {"OUT-001", "OUT-002"} <= set(d.rule_ids)


@pytest.mark.parametrize("kw", [{"fail": True}, {"unavailable": True}])
def test_estimator_outage_or_unavailable_plan_blocks_rather_than_guessing(svc, allow, kw):
    d = analyze(svc, *SELECTIVE, estimator=fakes.FakePlanEstimator(allow, **kw))
    assert d.decision == "BLOCK" and "EST-001" in d.rule_ids


def test_no_estimator_configured_blocks(svc):
    d = run(svc.analyze_query(Req(*SELECTIVE), fakes.state(), None))
    assert d.decision == "BLOCK" and d.rule_ids[0] == "EST-001"


def test_sync_estimator_with_foreign_model_is_accepted(svc, allow):
    precomputed = run(fakes.FakePlanEstimator(allow).estimate(SELECTIVE[0], [42], "inventory-lab")).model_dump(mode="json")

    class SyncDictEstimator:  # e.g. a synchronous adapter returning a plain dict
        def estimate(self, sql, params, target):
            return dict(precomputed)

    assert analyze(svc, *SELECTIVE, estimator=SyncDictEstimator()).decision == "ALLOW"


def test_blocked_candidate_never_reaches_estimator_or_learner(svc, allow):
    est = fakes.FakePlanEstimator(allow)
    d = analyze(svc, "DELETE FROM lab.products", estimator=est)
    assert d.decision == "BLOCK" and d.rule_ids == ["SQL-004"]
    assert est.calls == 0
    diag = svc.learner.diagnostics()
    assert svc.learner.status().pending_count == 0 and diag["counters"]["arrivals"] == 0
    assert diag["counters"]["decisions_block"] == 1


def test_policy_block_does_not_create_pending_correlation(svc):
    analyze(svc, *BROAD)
    assert svc.learner.status().pending_count == 0


def test_allowed_fixture_query_is_parked_for_its_outcome(svc):
    analyze(svc, *SELECTIVE)
    assert svc.learner.status().pending_count == 1


def test_live_learner_ignores_fixture_traffic(allow):
    live = IntelligenceService(allow)  # default learner accepts only source="live"
    analyze(live, *SELECTIVE)
    assert live.learner.status().pending_count == 0
    assert live.learner.diagnostics()["counters"]["rejected_source"] == 1


def test_stale_and_missing_state_require_review(svc):
    assert analyze(svc, *SELECTIVE, state=fakes.state(stale=True)).rule_ids == ["STATE-001"]
    d = analyze(svc, *SELECTIVE, state=None)
    assert d.decision == "WARN" and d.state_id == "unavailable"


def test_stale_state_can_be_configured_to_block(allow):
    svc = IntelligenceService(allow, policy_config=QueryPolicyConfig(stale_state_decision="BLOCK"))
    assert analyze(svc, *SELECTIVE, state=fakes.state(stale=True)).decision == "BLOCK"


def test_connection_pressure_tightens_decision(svc):
    assert analyze(svc, *SELECTIVE, state=fakes.state(db_used=42, db_limit=50)).rule_ids == ["STATE-002"]
    d = analyze(svc, *SELECTIVE, state=fakes.state(db_used=49, db_limit=50))
    assert d.decision == "BLOCK" and d.rule_ids == ["STATE-003"]


def test_unknown_connection_metrics_do_not_count_as_zero(svc):
    d = analyze(svc, *SELECTIVE, state=fakes.state(db_used=None))
    assert d.decision == "ALLOW"
    assert any("Heuristic risk" in r for r in d.reasons)


def test_active_chaos_run_pauses_sql(svc):
    d = analyze(svc, *SELECTIVE, state=fakes.state(active_fault_id="run_x"))
    assert d.decision == "BLOCK" and "STATE-004" in d.rule_ids


def test_stale_planner_statistics_warn(svc, allow):
    d = analyze(svc, *SELECTIVE, estimator=fakes.FakePlanEstimator(allow, stats_quality="stale"))
    assert d.rule_ids == ["EST-002"]


class _ConfidentLearner:
    """Stands in for a warm learner with a fixed prediction."""

    def __init__(self, real, ms):
        self.real, self.ms = real, ms

    def predict(self, features):
        return Prediction(duration_ms=self.ms, learner_version=LEARNER_VERSION, pre_update_sample_count=500, cold_start=False, drift_status="stable", quality_note="test")

    def __getattr__(self, name):
        return getattr(self.real, name)

    @staticmethod
    def trusted_prediction_ms(p):
        return p.duration_ms


def test_prediction_can_tighten_but_never_loosen(svc):
    real = svc.learner
    svc.learner = _ConfidentLearner(real, 60_000)
    d = analyze(svc, *SELECTIVE)
    assert d.decision == "WARN" and d.rule_ids == ["PRED-001"]
    svc.learner = _ConfidentLearner(real, 0.1)  # "very fast" cannot unblock
    assert analyze(svc, *BROAD).decision == "BLOCK"
    assert analyze(svc, "DELETE FROM lab.products").decision == "BLOCK"


def test_cold_start_prediction_is_not_used_by_policy(svc):
    d = analyze(svc, *SELECTIVE)
    assert d.prediction.cold_start and d.prediction.duration_ms is None and d.prediction.drift_status == "cold_start"
    assert "PRED-001" not in d.rule_ids


def test_policy_limits_are_immutable():
    cfg = QueryPolicyConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.hard_max_rows = 10**9  # type: ignore[misc]


def test_every_rule_id_emitted_is_documented(svc):
    for sql, params in [SELECTIVE, BROAD, ("SELECT id FROM lab.products ORDER BY name LIMIT 5", {})]:
        for rid in analyze(svc, sql, params).rule_ids:
            assert rid in RULES


def test_literals_and_parameters_never_leave_the_transient_path(svc, caplog):
    secret_literal, secret_param = "SECRET-LITERAL-7f3a", "SECRET-PARAM-91bc"
    caplog.set_level(logging.DEBUG)
    decisions = [
        analyze(svc, f"SELECT id FROM lab.products WHERE name = '{secret_literal}' AND sku = $1", {"1": secret_param}),
        analyze(svc, f"SELECT id FROM lab.products WHERE name = '{secret_literal}' AND sku = $1; DROP TABLE x", {"1": secret_param}),
        analyze(svc, f"SELECT '{secret_literal}' FRM lab.products", {}),
    ]
    blobs = [d.model_dump_json() for d in decisions]
    blobs += [caplog.text, svc.learner.status().model_dump_json(), repr(svc.learner.diagnostics()), learner_dump(svc.learner)]
    for blob in blobs:
        assert secret_literal not in blob and secret_param not in blob
    assert decisions[0].decision != "BLOCK" and svc.learner.status().pending_count == 1  # it really reached the learner
