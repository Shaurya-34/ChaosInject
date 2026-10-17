"""Probe, phases, state, recovery, plan estimation, learning, summary, redaction."""

from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta

from backend.app.telemetry._contracts import (
    CleanupStatus,
    LearningKind,
    LearningObservation,
    Outcome,
    Phase,
    RecoveryStatus,
    Source,
)
from backend.app.telemetry.clock import mono, now_utc
from backend.app.telemetry.cluster import StaticClusterAdapter, all_workloads_ready
from backend.app.telemetry.config import (
    LearningChannelConfig,
    RecoveryConfig,
    SamplingConfig,
    WorkloadConfig,
)
from backend.app.telemetry.fakes import (
    ScriptedStep,
    ScriptedTransport,
    degraded_readout,
    healthy_readout,
    make_load_profile,
    make_phase_summary,
)
from backend.app.telemetry.learning import LearningChannel
from backend.app.telemetry.phases import MonoAnchor, PhaseRecorder
from backend.app.telemetry.plan_estimator import (
    PlanEstimator,
    StaticPlanSource,
)
from backend.app.telemetry.probe import ProbeRunner
from backend.app.telemetry.recovery import FrozenBaseline, RecoveryDetector
from backend.app.telemetry.redaction import (
    assert_no_statement_text,
    redact_exception,
)
from backend.app.telemetry.sampling import Sample
from backend.app.telemetry.state import StateBuilder, state_is_usable_for_safety
from backend.app.telemetry.summary import (
    RuntimeFacts,
    evidence_ref,
    finalize_run,
    verify_evidence_hash,
)
from backend.app.telemetry.workload import (
    UncalibratedWorkloadError,
    WorkloadCalibration,
    WorkloadDefinition,
    classify,
    execute,
)


def run(coro):
    return asyncio.run(coro)


def ok(t, op="browse", ms=100.0):
    return Sample(mono_t=t, operation=op, outcome=Outcome.SUCCESS, duration_ms=ms)


# ---------------------------------------------------------------- workload


def calibrated() -> WorkloadCalibration:
    return WorkloadCalibration(
        verified=True,
        product_ids=("P1", "P2"),
        markers={
            "browse": "OK-MARK",
            "product_view": "OK-MARK",
            "add_to_cart": "OK-MARK",
            "cart_read": "OK-MARK",
            "checkout": "OK-MARK",
        },
        add_to_cart_form={"product_id": "{product_id}", "quantity": "1"},
    )


class WorkloadTests(unittest.TestCase):
    def test_uncalibrated_workload_refuses_to_build_strictly(self):
        with self.assertRaises(UncalibratedWorkloadError):
            WorkloadDefinition.build(WorkloadCalibration.unverified_default())

    def test_http_200_with_error_page_is_not_success(self):
        definition = WorkloadDefinition.build(calibrated())
        spec = definition.operations["cart_read"]
        transport = ScriptedTransport([ScriptedStep(200, "<h1>Error</h1> OK-MARK")])
        result = run(execute(spec, transport, {"product_id": "P1"}))
        self.assertEqual(result.outcome, Outcome.SEMANTIC_FAILURE)

    def test_missing_success_marker_is_failure(self):
        spec = WorkloadDefinition.build(calibrated()).operations["browse"]
        transport = ScriptedTransport([ScriptedStep(200, "blank page")])
        result = run(execute(spec, transport, {"product_id": "P1"}))
        self.assertEqual(result.outcome, Outcome.SEMANTIC_FAILURE)

    def test_marker_present_is_success(self):
        spec = WorkloadDefinition.build(calibrated()).operations["browse"]
        transport = ScriptedTransport([ScriptedStep(200, "OK-MARK", 42.0)])
        result = run(execute(spec, transport, {"product_id": "P1"}))
        self.assertEqual(result.outcome, Outcome.SUCCESS)
        self.assertEqual(result.duration_ms, 42.0)

    def test_5xx_is_http_error(self):
        spec = WorkloadDefinition.build(calibrated()).operations["browse"]
        from backend.app.telemetry.transport import ProbeResponse

        self.assertEqual(
            classify(spec, ProbeResponse(503, "OK-MARK", 5.0)), Outcome.HTTP_ERROR
        )

    def test_timeout_has_no_duration(self):
        spec = WorkloadDefinition.build(calibrated()).operations["browse"]
        transport = ScriptedTransport([ScriptedStep(raise_timeout=True)])
        result = run(execute(spec, transport, {"product_id": "P1"}))
        self.assertEqual(result.outcome, Outcome.TIMEOUT)
        self.assertIsNone(result.duration_ms)

    def test_result_carries_no_body_or_cookie(self):
        spec = WorkloadDefinition.build(calibrated()).operations["browse"]
        transport = ScriptedTransport([ScriptedStep(200, "OK-MARK secret-body")])
        result = run(execute(spec, transport, {"product_id": "P1"}))
        self.assertNotIn("secret-body", repr(result))


class ProbeRunnerTests(unittest.TestCase):
    def test_probe_emits_numeric_samples_and_closes_sessions(self):
        transports: list[ScriptedTransport] = []

        def factory():
            t = ScriptedTransport([ScriptedStep(200, "OK-MARK", 10.0)])
            transports.append(t)
            return t

        samples: list[Sample] = []
        runner = ProbeRunner(
            WorkloadDefinition.build(calibrated()),
            factory,
            samples.append,
            WorkloadConfig(requested_rate_rps=100.0, concurrency=4),
            seed=1,
        )
        stats = run(runner.run_for(0.5))
        self.assertGreater(stats.attempts, 10)
        self.assertEqual(len(samples), stats.attempts)
        self.assertTrue(all(s.outcome is Outcome.SUCCESS for s in samples))
        self.assertTrue(all(t.closed for t in transports))
        self.assertIsNotNone(stats.achieved_rate_rps)


# ---------------------------------------------------------------- phases

T0 = now_utc()


def recorder() -> PhaseRecorder:
    return PhaseRecorder(
        anchor=MonoAnchor(utc_at_anchor=T0, mono_at_anchor=0.0),
        sampling=SamplingConfig(ring_capacity=5000, ring_window_seconds=1e6),
        source=Source.FIXTURE,
    )


class PhaseWindowTests(unittest.TestCase):
    def test_samples_outside_the_window_are_excluded(self):
        rec = recorder()
        profile = make_load_profile()
        handle = rec.begin_phase(
            "r1", Phase.FAULT, profile, at_utc=T0 + timedelta(seconds=10)
        )
        for t in (5.0, 9.99, 10.0, 15.0, 20.0, 20.01, 30.0):
            rec.sink(ok(t))
        summary = rec.close_phase(handle, at_utc=T0 + timedelta(seconds=20))
        self.assertEqual(summary.overall.attempted, 3)  # 10.0, 15.0, 20.0

    def test_retroactive_open_replays_buffered_samples(self):
        rec = recorder()
        for t in range(0, 30):
            rec.sink(ok(float(t)))
        handle = rec.begin_phase(
            "r1", Phase.FAULT, make_load_profile(), at_utc=T0 + timedelta(seconds=10)
        )
        summary = rec.close_phase(handle, at_utc=T0 + timedelta(seconds=19))
        self.assertEqual(summary.overall.attempted, 10)

    def test_low_sample_percentiles_are_null_with_reason(self):
        rec = recorder()
        handle = rec.begin_phase(
            "r1", Phase.BASELINE, make_load_profile(), at_utc=T0
        )
        for t in range(50):
            rec.sink(ok(float(t) / 10))
        summary = rec.close_phase(handle, at_utc=T0 + timedelta(seconds=10))
        self.assertIsNone(summary.overall.p95_ms)
        self.assertEqual(summary.overall.succeeded, 50)
        self.assertTrue(any("below the configured minimum" in m for m in summary.overall.missing))

    def test_enough_samples_publish_percentiles(self):
        rec = recorder()
        handle = rec.begin_phase("r1", Phase.BASELINE, make_load_profile(), at_utc=T0)
        for t in range(200):
            rec.sink(ok(t / 20, ms=100.0))
        summary = rec.close_phase(handle, at_utc=T0 + timedelta(seconds=11))
        self.assertIsNotNone(summary.overall.p95_ms)
        self.assertAlmostEqual(summary.overall.p95_ms, 100.0, delta=5.0)

    def test_zero_traffic_phase_is_unknown_not_perfect(self):
        rec = recorder()
        handle = rec.begin_phase("r1", Phase.FAULT, make_load_profile(), at_utc=T0)
        summary = rec.close_phase(handle, at_utc=T0 + timedelta(seconds=10))
        self.assertEqual(summary.overall.attempted, 0)
        self.assertIsNone(summary.overall.success_rate)
        self.assertIsNone(summary.overall.p95_ms)

    def test_late_end_marker_does_not_leak_later_samples_into_the_window(self):
        """The marker arrives after the samples it should exclude."""
        rec = recorder()
        handle = rec.begin_phase(
            "r1", Phase.FAULT, make_load_profile(), at_utc=T0 + timedelta(seconds=10)
        )
        for t in range(10, 40):  # load keeps flowing past the 20s marker
            rec.sink(ok(float(t)))
        summary = rec.close_phase(handle, at_utc=T0 + timedelta(seconds=20))
        self.assertEqual(summary.overall.attempted, 11)  # 10..20 inclusive

    def test_eviction_past_window_start_is_disclosed(self):
        rec = PhaseRecorder(
            anchor=MonoAnchor(utc_at_anchor=T0, mono_at_anchor=0.0),
            sampling=SamplingConfig(ring_capacity=5, ring_window_seconds=1e6),
            source=Source.FIXTURE,
        )
        handle = rec.begin_phase("r1", Phase.FAULT, make_load_profile(), at_utc=T0)
        for t in range(20):
            rec.sink(ok(float(t)))
        summary = rec.close_phase(handle, at_utc=T0 + timedelta(seconds=10))
        self.assertTrue(any("already evicted" in n for n in summary.quality_notes))

    def test_duplicate_open_phase_is_refused(self):
        rec = recorder()
        rec.begin_phase("r1", Phase.FAULT, make_load_profile(), at_utc=T0)
        with self.assertRaises(ValueError):
            rec.begin_phase("r1", Phase.FAULT, make_load_profile(), at_utc=T0)

    def test_unmeasured_clock_alignment_is_disclosed(self):
        rec = recorder()
        handle = rec.begin_phase("r1", Phase.FAULT, make_load_profile(), at_utc=T0)
        summary = rec.close_phase(handle, at_utc=T0 + timedelta(seconds=1))
        self.assertIn("unmeasured", summary.clock_uncertainty)

    def test_readiness_tracks_minimum_not_average(self):
        rec = recorder()
        handle = rec.begin_phase("r1", Phase.FAULT, make_load_profile(), at_utc=T0)
        rec.observe_readiness(healthy_readout())
        rec.observe_readiness(degraded_readout("redis-cart"))
        rec.observe_readiness(healthy_readout())
        summary = rec.close_phase(handle, at_utc=T0 + timedelta(seconds=1))
        self.assertEqual(summary.ready_min["redis-cart"].ready, 0)
        self.assertEqual(summary.ready_max["redis-cart"].ready, 1)


# ---------------------------------------------------------------- state


class StateTests(unittest.TestCase):
    def _builder(self, samples, readout=None):
        rec = recorder()
        for s in samples:
            rec.ring.add(s)
        return StateBuilder(
            "bosch-navya",
            rec.ring,
            StaticClusterAdapter(readout or healthy_readout()),
            source=Source.FIXTURE,
        )

    def test_no_samples_means_unknown_not_healthy(self):
        snap = run(self._builder([]).capture())
        self.assertIsNone(snap.error_rate)
        self.assertIsNone(snap.p95_ms)
        self.assertIsNone(snap.request_rate)
        self.assertTrue(any("no samples" in m for m in snap.missing_features))

    def test_missing_resource_metrics_are_null_and_named(self):
        snap = run(self._builder([ok(mono())]).capture())
        self.assertIsNone(snap.cpu_utilization)
        self.assertTrue(any("cpu_utilization" in m for m in snap.missing_features))

    def test_snapshot_expires(self):
        snap = run(self._builder([ok(mono())]).capture())
        self.assertTrue(snap.is_fresh(at=snap.captured_at))
        late = snap.valid_until + timedelta(seconds=1)
        self.assertFalse(snap.is_fresh(at=late))
        usable, reason = state_is_usable_for_safety(snap, at=late)
        self.assertFalse(usable)
        self.assertIn("expired", reason)

    def test_incomplete_snapshot_is_not_usable_for_safety(self):
        snap = run(self._builder([ok(mono())]).capture())  # 1 sample -> p95 null
        usable, reason = state_is_usable_for_safety(snap, at=snap.captured_at)
        self.assertFalse(usable)
        self.assertIn("p95_ms", reason)

    def test_error_rate_computed_when_samples_present(self):
        now = mono()
        # 180 successes clears the 100-sample percentile minimum; 20 errors -> 10%.
        samples = [ok(now - 1)] * 180 + [
            Sample(now - 1, "browse", Outcome.HTTP_ERROR, 10.0)
        ] * 20
        snap = run(self._builder(samples).capture())
        self.assertAlmostEqual(snap.error_rate, 0.10, places=3)
        self.assertIsNotNone(snap.p95_ms)

    def test_active_fault_is_carried(self):
        b = self._builder([ok(mono())])
        b.set_active_fault("fault_123")
        self.assertEqual(run(b.capture()).active_fault_id, "fault_123")

    def test_empty_cluster_is_not_ready(self):
        self.assertFalse(all_workloads_ready({}))


# ---------------------------------------------------------------- recovery

BASE = FrozenBaseline(success_rate=0.99, p95_ms=100.0, sample_count=500)
GOOD = dict(success_rate=0.99, p95_ms=110.0, critical_successes=10)


def detector(**cfg):
    return RecoveryDetector(BASE, RecoveryConfig(**cfg))


class RecoveryTests(unittest.TestCase):
    def test_sustained_recovery_dates_from_first_healthy_window(self):
        d = detector(consecutive_healthy_windows=3)
        self.assertIsNone(d.observe_window(elapsed_seconds=10, readout=degraded_readout(), **GOOD))
        self.assertIsNone(d.observe_window(elapsed_seconds=20, readout=healthy_readout(), **GOOD))
        self.assertIsNone(d.observe_window(elapsed_seconds=30, readout=healthy_readout(), **GOOD))
        out = d.observe_window(elapsed_seconds=40, readout=healthy_readout(), **GOOD)
        self.assertEqual(out.status, RecoveryStatus.VERIFIED)
        self.assertEqual(out.recovery_seconds, 20)

    def test_ready_replicas_alone_do_not_prove_recovery(self):
        d = detector(consecutive_healthy_windows=2)
        for i in range(1, 4):
            d.observe_window(
                elapsed_seconds=i * 10,
                success_rate=0.2,
                p95_ms=100.0,
                critical_successes=0,
                readout=healthy_readout(),
            )
        self.assertNotEqual(d.pending().status, RecoveryStatus.VERIFIED)

    def test_a_relapse_resets_the_streak(self):
        d = detector(consecutive_healthy_windows=3)
        h = healthy_readout()
        d.observe_window(elapsed_seconds=10, readout=h, **GOOD)
        d.observe_window(elapsed_seconds=20, readout=h, **GOOD)
        d.observe_window(elapsed_seconds=30, readout=degraded_readout(), **GOOD)
        self.assertIsNone(d.observe_window(elapsed_seconds=40, readout=h, **GOOD))
        self.assertEqual(d.pending().status, RecoveryStatus.PENDING)

    def test_timeout_is_an_observed_failure(self):
        d = detector(timeout_seconds=30.0, consecutive_healthy_windows=3)
        out = d.observe_window(
            elapsed_seconds=30, readout=degraded_readout(), **GOOD
        )
        self.assertEqual(out.status, RecoveryStatus.FAILED)
        self.assertIsNone(out.recovery_seconds)

    def test_abandoned_observation_is_unknown_not_failed(self):
        out = detector().abandon("probe died")
        self.assertEqual(out.status, RecoveryStatus.UNKNOWN)

    def test_missing_p95_does_not_confirm_latency_recovery(self):
        d = detector(consecutive_healthy_windows=1)
        out = d.observe_window(
            elapsed_seconds=10,
            success_rate=0.99,
            p95_ms=None,
            critical_successes=10,
            readout=healthy_readout(),
        )
        self.assertIsNone(out)

    def test_baseline_is_frozen(self):
        with self.assertRaises(Exception):
            BASE.success_rate = 0.5  # type: ignore[misc]


# ---------------------------------------------------------------- plan estimator


def plan(root):
    return [{"Plan": root}]


class PlanEstimatorTests(unittest.TestCase):
    SQL = "SELECT secret_col FROM private_table WHERE id = $1"

    def _estimate(self, source):
        est = PlanEstimator(source, result_source=Source.FIXTURE)
        return run(est.estimate(self.SQL, [12345], "target_1"))

    def test_aggregate_over_big_scan_is_not_cheap(self):
        source = StaticPlanSource(
            plan(
                {
                    "Node Type": "Aggregate",
                    "Plan Rows": 1,
                    "Plan Width": 8,
                    "Total Cost": 90000.0,
                    "Plans": [{"Node Type": "Seq Scan", "Plan Rows": 5_000_000, "Plan Width": 8}],
                }
            )
        )
        e = self._estimate(source)
        self.assertEqual(e.estimated_rows, 1)
        self.assertEqual(e.max_node_estimated_rows, 5_000_000)
        self.assertTrue(any("scanned" in u or "aggregate" in u for u in e.uncertainty))

    def test_limit_over_sort_is_flagged(self):
        source = StaticPlanSource(
            plan(
                {
                    "Node Type": "Limit",
                    "Plan Rows": 10,
                    "Plan Width": 40,
                    "Total Cost": 5000.0,
                    "Plans": [
                        {
                            "Node Type": "Sort",
                            "Plan Rows": 2_000_000,
                            "Plan Width": 40,
                            "Plans": [{"Node Type": "Seq Scan", "Plan Rows": 2_000_000}],
                        }
                    ],
                }
            )
        )
        e = self._estimate(source)
        self.assertTrue(any("LIMIT" in u for u in e.uncertainty))
        self.assertEqual(e.max_node_estimated_rows, 2_000_000)

    def test_cost_is_labelled_not_a_duration(self):
        source = StaticPlanSource(plan({"Node Type": "Seq Scan", "Plan Rows": 5, "Plan Width": 4, "Total Cost": 1.5}))
        e = self._estimate(source)
        self.assertTrue(any("not a duration" in u for u in e.uncertainty))
        self.assertEqual(e.planner_cost, 1.5)

    def test_output_bytes_documents_overhead(self):
        source = StaticPlanSource(plan({"Node Type": "Seq Scan", "Plan Rows": 100, "Plan Width": 10, "Total Cost": 1.0}))
        e = self._estimate(source)
        self.assertAlmostEqual(e.estimated_output_bytes, 100 * 10 * 1.15, places=1)
        self.assertTrue(any("overhead" in u for u in e.uncertainty))

    def test_unavailable_planner_is_explicit_unknown_not_zero(self):
        e = self._estimate(StaticPlanSource(raise_unavailable="statement_timeout"))
        self.assertEqual(e.plan_source, "unavailable")
        self.assertIsNone(e.estimated_rows)
        self.assertIsNone(e.planner_cost)

    def test_malformed_plan_is_unavailable(self):
        e = self._estimate(StaticPlanSource([{"Nope": 1}]))
        self.assertEqual(e.plan_source, "unavailable")

    def test_stale_stats_widen_uncertainty(self):
        source = StaticPlanSource(
            plan({"Node Type": "Seq Scan", "Plan Rows": 5, "Plan Width": 4, "Total Cost": 1.0}),
            stats_hours=100.0,
        )
        e = self._estimate(source)
        self.assertEqual(e.stats_quality, "stale")

    def test_estimate_contains_no_sql_or_parameters(self):
        source = StaticPlanSource(
            plan({"Node Type": "Index Scan", "Plan Rows": 1, "Plan Width": 4, "Total Cost": 1.0,
                  "Filter": "(secret_col = 12345)", "Index Cond": "(id = 12345)"})
        )
        dumped = self._estimate(source).model_dump_json()
        for leaked in ("secret_col", "private_table", "12345", "Filter", "Index Cond"):
            self.assertNotIn(leaked, dumped)

    def test_driver_failure_text_is_not_propagated(self):
        class Leaky(StaticPlanSource):
            async def explain_json(self, sql, parameters, *, timeout_ms):
                raise RuntimeError(f"syntax error near: {sql}")

        e = self._estimate(Leaky())
        self.assertNotIn("secret_col", e.model_dump_json())
        self.assertEqual(e.estimation_error, "RuntimeError")


# ---------------------------------------------------------------- learning


class LearningChannelTests(unittest.TestCase):
    def test_persistence_cannot_be_enabled(self):
        with self.assertRaises(ValueError):
            LearningChannel(LearningChannelConfig(persistence_enabled=True))

    def test_outcome_joins_arrival_and_carries_no_features(self):
        ch = LearningChannel(source=Source.FIXTURE)
        ch.record_arrival("req_1", {"tables": 2.0}, None)
        out = ch.record_completed("req_1", duration_ms=12.0, rows_returned=3, bytes_returned=90)
        self.assertIsNone(out.features)
        self.assertEqual(ch.stats.joined_outcomes, 1)
        self.assertEqual(ch.pending_count, 0)

    def test_censored_requires_a_reason_and_is_not_completed(self):
        ch = LearningChannel(source=Source.FIXTURE)
        ch.record_arrival("req_2", {"x": 1.0}, None)
        out = ch.record_censored("req_2", observed_ms=4000.0, reason="row_cap_reached")
        self.assertIs(out.kind, LearningKind.CENSORED)
        with self.assertRaises(ValueError):
            LearningObservation(
                request_id="r", source=Source.FIXTURE, captured_at=now_utc(),
                kind=LearningKind.CENSORED,
            )

    def test_failed_or_blocked_has_no_duration(self):
        ch = LearningChannel(source=Source.FIXTURE)
        out = ch.record_failed("req_3")
        self.assertIsNone(out.duration_ms)
        with self.assertRaises(ValueError):
            LearningObservation(
                request_id="r", source=Source.FIXTURE, captured_at=now_utc(),
                kind=LearningKind.FAILED, duration_ms=5.0,
            )

    def test_arrival_without_features_is_rejected(self):
        with self.assertRaises(ValueError):
            LearningObservation(
                request_id="r", source=Source.FIXTURE, captured_at=now_utc(),
                kind=LearningKind.ARRIVAL,
            )

    def test_orphan_outcome_is_counted_not_invented(self):
        ch = LearningChannel(source=Source.FIXTURE)
        out = ch.record_completed("never_seen", duration_ms=1.0, rows_returned=0, bytes_returned=0)
        self.assertEqual(ch.stats.orphan_outcomes, 1)
        self.assertEqual(out.baseline_context, "unknown")

    def test_chaos_window_observations_are_labelled(self):
        ch = LearningChannel(source=Source.FIXTURE)
        rec = recorder()
        snap = run(
            StateBuilder("s", rec.ring, StaticClusterAdapter(healthy_readout()), source=Source.FIXTURE).capture()
        )
        faulted = snap.model_copy(update={"active_fault_id": "fault_9"})
        arrival = ch.record_arrival("req_4", {"x": 1.0}, faulted)
        self.assertEqual(arrival.baseline_context, "injected_fault")
        calm = ch.record_arrival("req_5", {"x": 1.0}, snap)
        self.assertEqual(calm.baseline_context, "baseline")

    def test_memory_bounded_over_a_long_stream_with_drop_counters(self):
        cfg = LearningChannelConfig(queue_capacity=100, pending_capacity=50)
        ch = LearningChannel(cfg, source=Source.FIXTURE)
        for i in range(5000):
            ch.record_arrival(f"req_{i}", {"x": float(i)}, None)
        self.assertLessEqual(len(ch), 100)
        self.assertLessEqual(ch.pending_count, 50)
        self.assertGreater(ch.stats.dropped_queue_full, 0)
        self.assertGreater(ch.stats.dropped_pending_full, 0)
        status = ch.status()
        self.assertEqual(status["persistence"], "disabled")
        self.assertEqual(status["retained_raw_examples"], 0)

    def test_drain_retains_nothing(self):
        ch = LearningChannel(source=Source.FIXTURE)
        ch.record_arrival("req_1", {"x": 1.0}, None)
        self.assertEqual(len(ch.drain()), 1)
        self.assertEqual(len(ch), 0)

    def test_no_files_are_written(self):
        import os, tempfile

        with tempfile.TemporaryDirectory() as d:
            old = os.getcwd()
            os.chdir(d)
            try:
                ch = LearningChannel(source=Source.FIXTURE)
                for i in range(500):
                    ch.record_arrival(f"r{i}", {"x": 1.0}, None)
                    ch.record_completed(f"r{i}", duration_ms=1.0, rows_returned=1, bytes_returned=1)
                self.assertEqual(os.listdir(d), [])
            finally:
                os.chdir(old)


# ---------------------------------------------------------------- summary


def make_summary():
    profile = make_load_profile()
    phases = {
        "baseline": make_phase_summary(Phase.BASELINE, load_profile=profile),
        "fault": make_phase_summary(Phase.FAULT, load_profile=profile),
    }
    facts = RuntimeFacts(
        catalog_id="EXP-01", sandbox_id="bosch-navya",
        stop_reason="ttl", cleanup_status=CleanupStatus.NOT_APPLICABLE,
    )
    return finalize_run("run_1", phases, profile, facts, None, source=Source.FIXTURE)


class SummaryTests(unittest.TestCase):
    def test_hash_verifies_and_detects_tampering(self):
        s = make_summary()
        self.assertTrue(s.evidence_hash.startswith("sha256:"))
        self.assertTrue(verify_evidence_hash(s))
        tampered = s.model_copy(update={"stop_reason": "something else"})
        self.assertFalse(verify_evidence_hash(tampered))

    def test_missing_recovery_is_unknown_not_verified(self):
        s = make_summary()
        self.assertIs(s.recovery_status, RecoveryStatus.UNKNOWN)
        self.assertIsNone(s.recovery_seconds)

    def test_evidence_ref_resolves_real_fields_only(self):
        s = make_summary()
        ref = evidence_ref(s, "/phase_summaries/fault/overall/attempted")
        self.assertEqual(ref.evidence_hash, s.evidence_hash)
        with self.assertRaises(KeyError):
            evidence_ref(s, "/phase_summaries/fault/invented_metric")

    def test_summary_is_immutable(self):
        s = make_summary()
        with self.assertRaises(Exception):
            s.stop_reason = "x"  # type: ignore[misc]


# ---------------------------------------------------------------- redaction


class RedactionTests(unittest.TestCase):
    def test_exception_text_with_sql_is_not_exported(self):
        exc = RuntimeError("syntax error at: SELECT ssn FROM people WHERE x=1")
        self.assertEqual(redact_exception(exc), "RuntimeError")

    def test_sqlstate_is_kept(self):
        class DbErr(Exception):
            sqlstate = "57014"

        self.assertEqual(redact_exception(DbErr("SELECT 1")), "DbErr[sqlstate=57014]")

    def test_statement_like_text_is_refused_for_export(self):
        with self.assertRaises(ValueError):
            assert_no_statement_text("select * from t", field="detail")
        assert_no_statement_text("plan_timeout", field="detail")


if __name__ == "__main__":
    unittest.main()
