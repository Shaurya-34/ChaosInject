from datetime import datetime, timedelta, timezone

from app.contracts import ExperimentProposal
from app.intelligence import fakes
from app.intelligence.planner import PlannerConfig, callers_of, rank_candidates

from .helpers import run


def ranked(state=None, catalog=None, coverage=None):
    now = datetime.now(timezone.utc)
    return rank_candidates(
        fakes.topology(now), state if state is not None else fakes.state(now), catalog or fakes.catalog(), coverage or {}, now, PlannerConfig()
    )


def by_key(cands):
    return {(c.catalog_id, c.target_id): c for c in cands}


def test_ranking_is_deterministic_and_sorted():
    a = [(c.catalog_id, c.target_id, c.score) for c in ranked()]
    assert a == [(c.catalog_id, c.target_id, c.score) for c in ranked()]
    scores = [s for _, _, s in a if s is not None]
    assert scores == sorted(scores, reverse=True)


def test_reverse_traversal_gives_blast_radius():
    assert set(callers_of(fakes.topology(), "redis-cart")) == {"cartservice", "frontend", "checkoutservice"}
    assert by_key(ranked())[("EXP-02", "redis-cart")].blast_radius == ("cartservice", "checkoutservice", "frontend")


def test_safety_exclusions_precede_ranking():
    cands = by_key(ranked())
    assert cands[("EXP-00", "redis-cart")].excluded_reason == "STATEFUL_TARGET"
    assert cands[("EXP-00", "redis-cart")].score is None
    assert cands[("EXP-03", "redis-cart")].excluded_reason == "CATALOG_UNAVAILABLE"
    assert ("EXP-02", "frontend") not in cands  # route faults only consider verified route targets


def test_no_proposals_when_state_is_stale_faulted_or_unhealthy(svc):
    topo, cat = fakes.topology(), fakes.catalog()
    for st in (fakes.state(stale=True), None, fakes.state(active_fault_id="run_1"), fakes.state(error_rate=0.2), fakes.state(error_rate=None)):
        assert run(svc.plan(topo, st, cat, {})) == []
    assert {c.excluded_reason for c in ranked(state=fakes.state(error_rate=None))} - {"STATEFUL_TARGET"} == {"BASELINE_UNKNOWN"}


def test_unready_target_is_excluded():
    c = by_key(ranked(state=fakes.state(not_ready=("currencyservice",))))
    assert c[("EXP-00", "currencyservice")].excluded_reason == "TARGET_NOT_READY"


def test_unavailable_catalog_yields_nothing(svc):
    assert run(svc.plan(fakes.topology(), fakes.state(), fakes.catalog(available=False), {})) == []


def test_coverage_lowers_priority_of_already_tested_pairs():
    fresh = by_key(ranked())[("EXP-02", "redis-cart")].score
    tested = by_key(ranked(coverage={("EXP-02", "redis-cart"): 3}))[("EXP-02", "redis-cart")].score
    assert tested < fresh


def test_proposals_are_bounded_contract_objects(svc):
    now = datetime.now(timezone.utc)
    props = run(svc.plan(fakes.topology(now), fakes.state(now), fakes.catalog(), {}))
    assert 0 < len(props) <= 5
    for p in props:
        assert isinstance(p, ExperimentProposal) and p.source == "fixture"
        assert p.fault_parameters.duration_seconds <= 60
        assert now < p.expires_at <= now + timedelta(minutes=16)
        assert p.hypothesis.startswith("Predicted:")


def test_hypothesis_uses_source_derived_soft_dependencies(svc):
    now = datetime.now(timezone.utc)
    props = run(svc.plan(fakes.topology(now), fakes.state(now), fakes.catalog(), {}))
    pc = next(p for p in props if p.target_id == "productcatalogservice")
    assert "browse" in pc.hypothesis and "checkout" in pc.hypothesis
