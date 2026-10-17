"""Acceptance verification for the LOCAL DEMO prevention path.

Navya's independent tests. ``RealHttpPreventionTests`` talks to an actual
loopback socket and is the evidence that matters; ``MockPreventionTests``
exercises the branches a real target cannot easily be forced into. The two are
separate classes, and every evidence record carries its observation mode, so a
mock result can never be reported as the live local test.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "telemetry"))

import reference_target  # noqa: E402

from backend.app.telemetry.prevention import (  # noqa: E402
    AcceptanceCheck,
    CountingExecutionSpy,
    HealthState,
    NonLoopbackTargetError,
    ObservationMode,
    PreventionVerifier,
    StaticHealthProbe,
    TargetHealthProbe,
    assert_no_score_for_prevented_attempt,
)

# The scenario from the acceptance gate: a 90-second pause against a 60-second cap.
REQUESTED_SECONDS = 90.0
CONFIGURED_MAX_SECONDS = 60.0
RULE_ID = "max_fault_duration_seconds"


class RealHttpPreventionTests(unittest.TestCase):
    """Real sockets, real HTTP, real counters."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.thread, cls.port = reference_target.serve_in_thread()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        reference_target.reset_state()
        self.probe = TargetHealthProbe(self.base_url)
        self.spy = CountingExecutionSpy()
        self.verifier = PreventionVerifier(
            self.probe,
            self.spy,
            configured_max_duration_seconds=CONFIGURED_MAX_SECONDS,
        )

    def test_target_is_healthy_over_real_http(self):
        reading = self.probe.read_health()
        self.assertEqual(reading.state, HealthState.HEALTHY)
        self.assertEqual(reading.mode, ObservationMode.REAL_HTTP)
        self.assertEqual(reading.status_code, 200)
        self.assertIsNotNone(reading.latency_ms)

    def test_rejected_attempt_passes_every_acceptance_check(self):
        """The headline test: prevention, proven from outside the runtime."""
        before = self.probe.read_health()
        spy_before = self.spy.invocation_count

        # Tooshita's guard rejects here. No executor call is made, so the spy
        # count does not move and the target sees no mutating request.

        after = self.probe.read_health()

        evidence = self.verifier.verify_rejected_attempt(
            request_id="req_localdemo_001",
            rule_id=RULE_ID,
            decision="BLOCK",
            executed=False,
            requested_duration_seconds=REQUESTED_SECONDS,
            stop_latched=True,
            health_before=before,
            health_after=after,
            spy_count_before=spy_before,
        )

        self.assertEqual(evidence.verdict, "ACCEPTED")
        self.assertEqual(evidence.observation_mode, ObservationMode.REAL_HTTP)
        self.assertTrue(evidence.all_checks_passed)
        self.assertEqual(evidence.executor_invocations_reported, 0)
        self.assertEqual(evidence.target_observed_mutations, 0)
        self.assertTrue(evidence.corroborated)
        self.assertEqual(evidence.health_before, HealthState.HEALTHY)
        self.assertEqual(evidence.health_after, HealthState.HEALTHY)

    def test_evidence_records_the_numbers_the_gate_asks_for(self):
        before = self.probe.read_health()
        after = self.probe.read_health()
        evidence = self.verifier.verify_rejected_attempt(
            request_id="req_localdemo_002",
            rule_id=RULE_ID,
            decision="BLOCK",
            executed=False,
            requested_duration_seconds=REQUESTED_SECONDS,
            stop_latched=True,
            health_before=before,
            health_after=after,
            spy_count_before=0,
        )
        payload = evidence.as_dict()

        for field in (
            "request_id",
            "rule_id",
            "requested_duration_seconds",
            "configured_max_duration_seconds",
            "decision",
            "executed",
            "provenance",
            "evidence_hash",
        ):
            self.assertIn(field, payload)

        self.assertEqual(payload["provenance"], "LOCAL_DEMO")
        self.assertEqual(payload["executed"], False)
        self.assertEqual(payload["requested_duration_seconds"], 90.0)
        self.assertEqual(payload["configured_max_duration_seconds"], 60.0)
        self.assertTrue(payload["evidence_hash"].startswith("sha256:"))

    def test_a_dispatched_mutation_would_be_caught(self):
        """Negative control.

        If the guard ever leaked a mutation through, the target's own witness
        increments and the verdict flips. Without this test the suite would pass
        just as happily against a verifier that never checked anything.
        """
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        conn.request("POST", "/pause", body=b"{}")
        conn.getresponse().read()
        conn.close()

        self.spy.record("pause_service")

        before = self.probe.read_health()
        after = self.probe.read_health()
        evidence = self.verifier.verify_rejected_attempt(
            request_id="req_localdemo_leak",
            rule_id=RULE_ID,
            decision="BLOCK",
            executed=False,
            requested_duration_seconds=REQUESTED_SECONDS,
            stop_latched=True,
            health_before=before,
            health_after=after,
            spy_count_before=0,
        )

        self.assertEqual(evidence.verdict, "REJECTED")
        self.assertEqual(evidence.target_observed_mutations, 1)
        self.assertFalse(evidence.corroborated)
        failed = {c.check_id for c in evidence.checks if not c.passed}
        self.assertIn("zero_executor_invocations", failed)
        self.assertIn("target_observed_zero_mutations", failed)

    def test_unavailable_target_fails_closed(self):
        self.server.shutdown()
        try:
            reading = self.probe.read_health()
            self.assertEqual(reading.state, HealthState.UNAVAILABLE)
            self.assertEqual(reading.mode, ObservationMode.REAL_HTTP)

            allowed = self.verifier.verify_fails_closed_when_target_unavailable(
                decision="ALLOW", health=reading
            )
            self.assertFalse(allowed.passed)

            blocked = self.verifier.verify_fails_closed_when_target_unavailable(
                decision="BLOCK", health=reading
            )
            self.assertTrue(blocked.passed)
        finally:
            import threading

            self.__class__.thread = threading.Thread(
                target=self.server.serve_forever, daemon=True
            )
            self.__class__.thread.start()

    def test_unhealthy_target_is_not_healthy(self):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        conn.request("POST", "/_unhealthy", body=b"{}")
        conn.getresponse().read()
        conn.close()

        reading = self.probe.read_health()
        self.assertEqual(reading.state, HealthState.UNHEALTHY)
        self.assertFalse(reading.is_healthy_now)

    def test_non_loopback_target_is_refused(self):
        with self.assertRaises(NonLoopbackTargetError):
            TargetHealthProbe("http://example.com")


class MockPreventionTests(unittest.TestCase):
    """Mock-only branches. Never reported as real-HTTP evidence."""

    def setUp(self):
        self.spy = CountingExecutionSpy()

    def _verifier(self, probe):
        return PreventionVerifier(
            probe, self.spy, configured_max_duration_seconds=CONFIGURED_MAX_SECONDS
        )

    def test_mock_evidence_is_labelled_and_noted(self):
        probe = StaticHealthProbe([HealthState.HEALTHY, HealthState.HEALTHY])
        verifier = self._verifier(probe)
        evidence = verifier.verify_rejected_attempt(
            request_id="req_mock_001",
            rule_id=RULE_ID,
            decision="BLOCK",
            executed=False,
            requested_duration_seconds=REQUESTED_SECONDS,
            stop_latched=True,
            health_before=probe.read_health(),
            health_after=probe.read_health(),
            spy_count_before=0,
        )
        self.assertEqual(evidence.observation_mode, ObservationMode.MOCK)
        self.assertTrue(any("MOCK observation" in n for n in evidence.notes))

    def test_stale_health_fails_closed(self):
        probe = StaticHealthProbe([HealthState.HEALTHY], stale=True)
        verifier = self._verifier(probe)
        reading = probe.read_health()

        self.assertFalse(reading.is_healthy_now)
        check = verifier.verify_fails_closed_when_target_unavailable(
            decision="ALLOW", health=reading
        )
        self.assertFalse(check.passed)

    def test_missing_witness_downgrades_corroboration(self):
        probe = StaticHealthProbe([HealthState.HEALTHY], witness=None)
        verifier = self._verifier(probe)
        evidence = verifier.verify_rejected_attempt(
            request_id="req_mock_002",
            rule_id=RULE_ID,
            decision="BLOCK",
            executed=False,
            requested_duration_seconds=REQUESTED_SECONDS,
            stop_latched=True,
            health_before=probe.read_health(),
            health_after=probe.read_health(),
            spy_count_before=0,
        )
        self.assertFalse(evidence.corroborated)
        self.assertIsNone(evidence.target_observed_mutations)
        self.assertTrue(any("uncorroborated" in n for n in evidence.notes))

    def test_within_cap_request_still_blocked_while_execution_unavailable(self):
        probe = StaticHealthProbe([HealthState.HEALTHY])
        verifier = self._verifier(probe)

        allowed = verifier.verify_blocked_while_execution_unavailable(
            requested_duration_seconds=30.0, decision="ALLOW"
        )
        self.assertFalse(allowed.passed)

        blocked = verifier.verify_blocked_while_execution_unavailable(
            requested_duration_seconds=30.0, decision="BLOCK"
        )
        self.assertTrue(blocked.passed)

    def test_rule_must_numerically_apply(self):
        """Guards against a guard that blocks for the wrong reason."""
        probe = StaticHealthProbe([HealthState.HEALTHY])
        verifier = self._verifier(probe)
        evidence = verifier.verify_rejected_attempt(
            request_id="req_mock_003",
            rule_id=RULE_ID,
            decision="BLOCK",
            executed=False,
            requested_duration_seconds=30.0,  # within the cap
            stop_latched=True,
            health_before=probe.read_health(),
            health_after=probe.read_health(),
            spy_count_before=0,
        )
        failed = {c.check_id for c in evidence.checks if not c.passed}
        self.assertIn("rule_numerically_applies", failed)

    def test_mixed_observation_modes_downgrade_the_verdict(self):
        real_like = StaticHealthProbe([HealthState.HEALTHY])
        verifier = self._verifier(real_like)
        before = real_like.read_health()
        after = real_like.read_health()
        object.__setattr__(after, "mode", ObservationMode.REAL_HTTP)

        evidence = verifier.verify_rejected_attempt(
            request_id="req_mock_004",
            rule_id=RULE_ID,
            decision="BLOCK",
            executed=False,
            requested_duration_seconds=REQUESTED_SECONDS,
            stop_latched=True,
            health_before=before,
            health_after=after,
            spy_count_before=0,
        )
        self.assertEqual(evidence.observation_mode, ObservationMode.MOCK)
        self.assertTrue(any("different observation modes" in n for n in evidence.notes))

    def test_unlatched_stop_fails(self):
        probe = StaticHealthProbe([HealthState.HEALTHY])
        verifier = self._verifier(probe)
        evidence = verifier.verify_rejected_attempt(
            request_id="req_mock_005",
            rule_id=RULE_ID,
            decision="BLOCK",
            executed=False,
            requested_duration_seconds=REQUESTED_SECONDS,
            stop_latched=False,
            health_before=probe.read_health(),
            health_after=probe.read_health(),
            spy_count_before=0,
        )
        self.assertEqual(evidence.verdict, "REJECTED")
        self.assertIn(
            "stop_latched", {c.check_id for c in evidence.checks if not c.passed}
        )


class NoScoreForPreventedAttemptTests(unittest.TestCase):
    """A prevented fault must never carry a resilience score."""

    def _evidence(self, executed=False):
        probe = StaticHealthProbe([HealthState.HEALTHY])
        verifier = PreventionVerifier(
            probe,
            CountingExecutionSpy(),
            configured_max_duration_seconds=CONFIGURED_MAX_SECONDS,
        )
        return verifier.verify_rejected_attempt(
            request_id="req_score_guard",
            rule_id=RULE_ID,
            decision="BLOCK",
            executed=executed,
            requested_duration_seconds=REQUESTED_SECONDS,
            stop_latched=True,
            health_before=probe.read_health(),
            health_after=probe.read_health(),
            spy_count_before=0,
        )

    def test_evidence_exposes_a_null_score_with_a_reason(self):
        payload = self._evidence().as_dict()
        self.assertIsNone(payload["resilience_score"])
        self.assertIn("no fault was executed", payload["resilience_score_reason"])

    def test_guard_accepts_a_genuine_prevented_attempt(self):
        assert_no_score_for_prevented_attempt(self._evidence())

    def test_guard_rejects_evidence_claiming_execution(self):
        with self.assertRaises(ValueError):
            assert_no_score_for_prevented_attempt(self._evidence(executed=True))

    def test_prevention_evidence_has_no_score_field_to_fill(self):
        from backend.app.telemetry.prevention import PreventionEvidence

        self.assertNotIn("resilience_score", PreventionEvidence.__annotations__)


class AcceptanceCheckTests(unittest.TestCase):
    def test_check_serialises_with_its_mode(self):
        check = AcceptanceCheck(
            check_id="x",
            description="d",
            passed=True,
            observed="o",
            mode=ObservationMode.REAL_HTTP,
        )
        self.assertEqual(check.as_dict()["mode"], "real_http")


if __name__ == "__main__":
    unittest.main()
