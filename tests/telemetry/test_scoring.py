"""Deterministic scoring, including every way a total must be withheld."""

from __future__ import annotations

import unittest

from backend.app.telemetry._contracts import CleanupStatus, Phase, RecoveryStatus
from backend.app.telemetry.fakes import (
    make_load_profile,
    make_operation_aggregate,
    make_phase_summary,
)
from backend.app.telemetry.scoring import (
    ResilienceScorer,
    SafetyChecks,
    ScoringInputs,
    aggregate,
)

PASSING_SAFETY = SafetyChecks(
    cleanup_status=CleanupStatus.VERIFIED,
    recovery_status=RecoveryStatus.VERIFIED,
    workflow_constraints_held=True,
)


def build_inputs(**overrides) -> ScoringInputs:
    baseline = overrides.pop(
        "baseline",
        make_phase_summary(
            Phase.BASELINE,
            overall=make_operation_aggregate(
                attempted=600, succeeded=599, p50_ms=80.0, p95_ms=180.0
            ),
        ),
    )
    fault = overrides.pop(
        "fault",
        make_phase_summary(
            Phase.FAULT,
            overall=make_operation_aggregate(
                attempted=450, succeeded=309, p50_ms=600.0, p95_ms=1200.0
            ),
        ),
    )
    defaults = dict(
        run_id="run_test",
        baseline=baseline,
        fault=fault,
        latency_abort_ms=3000.0,
        recovery_timeout_seconds=180.0,
        recovery_status=RecoveryStatus.VERIFIED,
        recovery_seconds=24.0,
        safety=PASSING_SAFETY,
    )
    defaults.update(overrides)
    return ScoringInputs(**defaults)


class ScoreArithmeticTests(unittest.TestCase):
    def setUp(self):
        self.scorer = ResilienceScorer()

    def test_components_match_the_published_formula(self):
        score = self.scorer.score(build_inputs())
        self.assertFalse(score.insufficient_evidence)

        by_name = {c.name: c for c in score.components}

        # A = 100 * 309/450
        self.assertAlmostEqual(by_name["availability"].value, 68.667, places=2)
        # L = 100 * (3000-1200)/(3000-180)
        self.assertAlmostEqual(by_name["latency"].value, 63.830, places=2)
        # R = 100 * (1 - 24/180)
        self.assertAlmostEqual(by_name["recovery"].value, 86.667, places=2)
        self.assertEqual(by_name["safety"].value, 100.0)

        expected = (
            0.40 * 68.667 + 0.20 * 63.830 + 0.25 * 86.667 + 0.15 * 100.0
        )
        self.assertAlmostEqual(score.total, round(expected, 2), delta=0.05)

    def test_latency_is_clipped_at_both_ends(self):
        faster = self.scorer.score(
            build_inputs(
                fault=make_phase_summary(
                    Phase.FAULT,
                    overall=make_operation_aggregate(
                        attempted=450, succeeded=450, p95_ms=100.0
                    ),
                )
            )
        )
        self.assertEqual(
            {c.name: c.value for c in faster.components}["latency"], 100.0
        )

        slower = self.scorer.score(
            build_inputs(
                fault=make_phase_summary(
                    Phase.FAULT,
                    overall=make_operation_aggregate(
                        attempted=450, succeeded=450, p95_ms=9000.0
                    ),
                )
            )
        )
        self.assertEqual({c.name: c.value for c in slower.components}["latency"], 0.0)

    def test_total_cannot_exceed_one_hundred(self):
        perfect = self.scorer.score(
            build_inputs(
                fault=make_phase_summary(
                    Phase.FAULT,
                    overall=make_operation_aggregate(
                        attempted=500, succeeded=500, p95_ms=180.0
                    ),
                ),
                recovery_seconds=0.0,
            )
        )
        self.assertLessEqual(perfect.total, 100.0)


class InsufficientEvidenceTests(unittest.TestCase):
    """The behaviour that keeps the score defensible."""

    def setUp(self):
        self.scorer = ResilienceScorer()

    def test_zero_fault_attempts_is_unknown_not_zero_availability(self):
        score = self.scorer.score(
            build_inputs(
                fault=make_phase_summary(
                    Phase.FAULT,
                    overall=make_operation_aggregate(
                        attempted=0, succeeded=0, p50_ms=None, p95_ms=None,
                        sample_count=0,
                    ),
                )
            )
        )
        availability = {c.name: c for c in score.components}["availability"]
        self.assertFalse(availability.known)
        self.assertIsNone(availability.value)
        self.assertIn("unknown, not zero", availability.reason)
        self.assertTrue(score.insufficient_evidence)
        self.assertIsNone(score.total)

    def test_missing_baseline_percentile_withholds_the_total(self):
        score = self.scorer.score(
            build_inputs(
                baseline=make_phase_summary(
                    Phase.BASELINE,
                    overall=make_operation_aggregate(
                        attempted=40, succeeded=40, p50_ms=None, p95_ms=None,
                        sample_count=40,
                    ),
                )
            )
        )
        self.assertTrue(score.insufficient_evidence)
        self.assertIsNone(score.total)

    def test_weights_are_never_renormalised(self):
        """The core anti-flattery rule.

        With latency unknown, a renormalising implementation would rescale the
        other three weights to sum to 1 and emit a confident-looking total.
        """
        score = self.scorer.score(
            build_inputs(
                fault=make_phase_summary(
                    Phase.FAULT,
                    overall=make_operation_aggregate(
                        attempted=450, succeeded=309, p50_ms=None, p95_ms=None,
                        sample_count=20,
                    ),
                )
            )
        )
        self.assertIsNone(score.total)
        self.assertTrue(score.insufficient_evidence)
        known = [c for c in score.components if c.known]
        self.assertEqual(sum(c.weight for c in known), 0.80)
        self.assertTrue(any("not renormalised" in g for g in score.guardrails))

    def test_observed_failure_to_recover_scores_zero(self):
        score = self.scorer.score(
            build_inputs(
                recovery_status=RecoveryStatus.FAILED,
                recovery_seconds=None,
                safety=SafetyChecks(
                    cleanup_status=CleanupStatus.VERIFIED,
                    recovery_status=RecoveryStatus.FAILED,
                    workflow_constraints_held=True,
                ),
            )
        )
        recovery = {c.name: c for c in score.components}["recovery"]
        self.assertTrue(recovery.known)
        self.assertEqual(recovery.value, 0.0)
        self.assertIsNotNone(score.total)

    def test_lost_recovery_observation_is_unknown_not_zero(self):
        """The distinction that stops an abandoned run looking like a failure."""
        score = self.scorer.score(
            build_inputs(
                recovery_status=RecoveryStatus.UNKNOWN,
                recovery_seconds=None,
                safety=SafetyChecks(
                    cleanup_status=CleanupStatus.VERIFIED,
                    recovery_status=RecoveryStatus.UNKNOWN,
                    workflow_constraints_held=True,
                ),
            )
        )
        recovery = {c.name: c for c in score.components}["recovery"]
        self.assertFalse(recovery.known)
        self.assertIsNone(recovery.value)
        self.assertTrue(score.insufficient_evidence)

    def test_degenerate_latency_range_is_rejected(self):
        score = self.scorer.score(build_inputs(latency_abort_ms=100.0))
        latency = {c.name: c for c in score.components}["latency"]
        self.assertFalse(latency.known)
        self.assertIn("degenerate", latency.reason)

    def test_components_are_shown_even_without_a_total(self):
        score = self.scorer.score(
            build_inputs(recovery_status=RecoveryStatus.UNKNOWN, recovery_seconds=None)
        )
        self.assertEqual(len(score.components), 4)
        self.assertIsNone(score.total)


class GuardrailTests(unittest.TestCase):
    def setUp(self):
        self.scorer = ResilienceScorer()

    def test_mismatched_load_profiles_block_the_total(self):
        score = self.scorer.score(
            build_inputs(
                fault=make_phase_summary(
                    Phase.FAULT,
                    load_profile=make_load_profile(
                        load_profile_id="different-profile", rate=4.0
                    ),
                    overall=make_operation_aggregate(attempted=450, succeeded=309),
                )
            )
        )
        self.assertTrue(score.insufficient_evidence)
        self.assertTrue(any("not comparable" in g for g in score.guardrails))

    def test_unhealthy_baseline_blocks_the_total(self):
        score = self.scorer.score(
            build_inputs(
                baseline=make_phase_summary(
                    Phase.BASELINE,
                    overall=make_operation_aggregate(attempted=600, succeeded=400),
                )
            )
        )
        self.assertTrue(score.insufficient_evidence)
        self.assertTrue(any("unsound" in g for g in score.guardrails))

    def test_failed_cleanup_zeroes_safety(self):
        score = self.scorer.score(
            build_inputs(
                safety=SafetyChecks(
                    cleanup_status=CleanupStatus.FAILED,
                    recovery_status=RecoveryStatus.VERIFIED,
                    workflow_constraints_held=True,
                )
            )
        )
        self.assertEqual({c.name: c for c in score.components}["safety"].value, 0.0)

    def test_not_applicable_cleanup_still_passes_safety(self):
        """Deleted pods cannot be 'restored', so NOT_APPLICABLE is not a failure."""
        score = self.scorer.score(
            build_inputs(
                safety=SafetyChecks(
                    cleanup_status=CleanupStatus.NOT_APPLICABLE,
                    recovery_status=RecoveryStatus.VERIFIED,
                    workflow_constraints_held=True,
                )
            )
        )
        self.assertEqual({c.name: c for c in score.components}["safety"].value, 100.0)


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.scorer = ResilienceScorer()

    def test_aggregate_requires_complete_coverage(self):
        good = self.scorer.score(build_inputs(run_id="run_a"))
        bad = self.scorer.score(
            build_inputs(
                run_id="run_b",
                recovery_status=RecoveryStatus.UNKNOWN,
                recovery_seconds=None,
            )
        )
        result = aggregate([good, bad])
        self.assertIsNone(result.mean_total)
        self.assertFalse(result.complete_coverage)
        self.assertEqual(result.excluded_runs, ("run_b",))

    def test_aggregate_of_valid_runs_is_an_equal_weight_mean(self):
        a = self.scorer.score(build_inputs(run_id="run_a"))
        b = self.scorer.score(build_inputs(run_id="run_b"))
        result = aggregate([a, b])
        self.assertTrue(result.complete_coverage)
        self.assertAlmostEqual(result.mean_total, (a.total + b.total) / 2, places=2)

    def test_no_experiments_yields_no_aggregate(self):
        self.assertIsNone(aggregate([]).mean_total)


if __name__ == "__main__":
    unittest.main()
