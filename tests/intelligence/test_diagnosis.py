from app.api.evidence import checked_evidence
from app.intelligence import fakes
from app.intelligence.dependency_map import classify
from app.intelligence.diagnosis import DiagnosisConfig, analyze, escape_pointer_token, resolve_pointer, summary_document

from .helpers import run

HEALTHY = {op: fakes.agg(120, 0, 60, 150) for op in fakes.OPERATIONS}


def diagnose(target="productcatalogservice", **kw):
    summary = fakes.run_summary(**kw)
    diag, _ = analyze(summary, fakes.topology(), DiagnosisConfig(), target)
    return summary, diag


def status(diag):
    return {h.hypothesis_id: h.status for h in diag.hypotheses}


def test_dependency_map_matches_vendored_source_reading():
    assert classify("checkout", "paymentservice") == "hard"
    assert classify("checkout", "emailservice") == "soft"
    assert classify("browse", "adservice") == "soft"
    assert classify("product_view", "recommendationservice") == "soft"
    assert classify("add_to_cart", "redis-cart") == "hard"
    assert classify("browse", "paymentservice") == "unrelated"


def test_service_output_passes_the_api_evidence_check(svc):
    summary = fakes.run_summary()
    svc.note_run_target(summary.run_id, "productcatalogservice")
    result = run(svc.analyze_run(summary, fakes.topology()))
    checked_evidence(summary, result)  # raises PortError on any bad reference or source mismatch
    assert result.source == summary.source and result.mode == "deterministic_fallback"
    assert any("labelled fixture" in lim for lim in result.limitations)


def test_measured_fact_pointers_resolve_to_the_quoted_values():
    summary, diag = diagnose()
    doc = summary_document(summary)
    for fact in diag.facts:
        assert resolve_pointer(doc, fact.pointer) == fact.value


def test_hard_dependency_failure_is_supported_and_ranked_first(svc):
    _, diag = diagnose("productcatalogservice")
    assert status(diag)["H-checkout-hard"] == "supported_in_this_run"
    assert next(h for h in diag.hypotheses if h.hypothesis_id == "H-checkout-hard").priority == 1
    assert {r.template_id for r in diag.remedies} == {"REM-GRACEFUL-DEGRADATION", "REM-DEADLINES-RETRY-BUDGET"}


def test_tolerated_soft_dependency_is_reported_as_resilience():
    fault = dict(HEALTHY)  # adservice paused, nothing degraded
    _, diag = diagnose("adservice", fault_ops=fault)
    assert status(diag)["H-browse-soft"] == "supported_in_this_run"
    assert next(h for h in diag.hypotheses if h.hypothesis_id == "H-browse-soft").priority == 5
    assert diag.remedies == ()


def test_failed_fallback_and_out_of_radius_degradation_are_flagged():
    _, diag = diagnose("adservice")  # every operation degraded
    s = status(diag)
    assert s["H-browse-soft"] == "not_supported"
    assert s["H-checkout-unexpected"] == "insufficient_evidence"
    assert any("outside the predicted blast radius" in a for a in diag.alternatives)
    assert "REM-FIX-FALLBACK" in {r.template_id for r in diag.remedies}


def test_unknown_target_reports_degradation_without_attribution(svc):
    summary = fakes.run_summary()
    result = run(svc.analyze_run(summary, fakes.topology()))
    assert any("does not record the injected target" in lim for lim in result.limitations)
    assert all(h.status == "supported_in_this_run" for h in result.hypotheses)
    assert all("degraded during the fault" in h.title for h in result.hypotheses)
    checked_evidence(summary, result)


def test_no_degradation_on_hard_dependency_is_not_supported():
    _, diag = diagnose(fault_ops=dict(HEALTHY))
    assert set(status(diag).values()) == {"not_supported"}
    assert diag.remedies == ()


def test_low_sample_counts_are_insufficient_evidence():
    _, diag = diagnose(samples=5)
    assert set(status(diag).values()) == {"insufficient_evidence"}


def test_redis_advice_does_not_present_a_plain_replica_as_ha():
    _, diag = diagnose("redis-cart", catalog_id="EXP-02")
    rec = next(r for r in diag.remedies if r.template_id == "REM-CART-STORE-HA")
    assert any("is not high availability" in t for t in rec.tradeoffs)
    assert "REM-REPLICAS-PROBES" not in {r.template_id for r in diag.remedies}


def test_pdb_is_never_claimed_to_stop_direct_pod_deletion():
    _, diag = diagnose("cartservice", catalog_id="EXP-01")
    rec = next(r for r in diag.remedies if r.template_id == "REM-REPLICAS-PROBES")
    assert any("does not prevent direct Pod deletion" in t for t in rec.tradeoffs)


def test_failed_and_incomplete_recovery():
    bad_recovery = {op: fakes.agg(120, 30, 70, 400) for op in fakes.OPERATIONS}
    _, diag = diagnose(recovery_status="failed", recovery_seconds=120.0, recovery_ops=bad_recovery)
    assert status(diag)["H-checkout-incomplete-recovery"] == "supported_in_this_run"
    rec = next(r for r in diag.remedies if r.template_id == "REM-RECOVERY-PROBES")
    assert {"/recovery_status", "/recovery_seconds"} <= set(rec.pointers)


def test_unverified_recovery_is_a_limitation_not_zero():
    _, diag = diagnose(recovery_status="unknown", recovery_seconds=None)
    assert any("unknown, not zero" in lim for lim in diag.limitations)


def test_contract_findings_are_ranked_and_predicted(svc):
    summary = fakes.run_summary(catalog_id="EXP-02")
    svc.note_run_target(summary.run_id, "redis-cart")
    result = run(svc.analyze_run(summary, fakes.topology()))
    assert all(r.status == "predicted" and 1 <= r.priority <= 5 for r in result.recommendations)
    assert [h.priority for h in result.hypotheses] == sorted(h.priority for h in result.hypotheses)
    assert all("no numeric benefit is claimed" in r.explanation for r in result.recommendations)


def test_pointer_escaping_handles_operation_names_with_slashes():
    assert escape_pointer_token("GET /cart~x") == "GET ~1cart~0x"
    assert resolve_pointer({"a/b": {"~": 1}}, "/a~1b/~0") == 1
