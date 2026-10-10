import subprocess
import time
import unittest
from unittest import mock

from chaos import faults
from chaos.controller import PolicyError, run

# These tests check controller mechanics, not safety rules, so the safety hook never stops.
NEVER_STOP = lambda sample, baseline, policy: None

POLICY = {"allowed_targets": ["web"], "allowed_faults": ["container_stop"],
          "max_duration_s": 60, "hard_timeout_s": 120, "recovery_timeout_s": 1}
EXP = {"name": "t", "target": "web", "fault": "container_stop", "duration_s": 0.2,
       "probe_url": "http://x", "baseline_samples": 2, "interval_s": 0.01}


class FakeFault:
    def __init__(self):
        self.down = False
        self.calls = []

    def inject(self, target):
        self.down = True
        self.calls.append("inject")

    def restore(self, target):
        self.down = False
        self.calls.append("restore")


def probe_for(fault):
    return lambda url: {"t": time.time(), "ok": not fault.down, "status": None, "latency_ms": 1.0}


class ControllerTest(unittest.TestCase):
    def test_refuses_target_not_allowlisted(self):
        with self.assertRaises(PolicyError):
            run({**EXP, "target": "other"}, POLICY, fault=FakeFault())

    def test_refuses_fault_not_allowlisted(self):
        with self.assertRaises(PolicyError):
            run({**EXP, "fault": "disk_fill"}, POLICY, fault=FakeFault())

    def test_refuses_duration_over_max(self):
        with self.assertRaises(PolicyError):
            run({**EXP, "duration_s": 61}, POLICY, fault=FakeFault())

    def test_runs_full_cycle_and_recovers(self):
        f = FakeFault()
        r = run(EXP, POLICY, fault=f, probe_fn=probe_for(f), should_stop=NEVER_STOP)
        self.assertEqual(f.calls, ["inject", "restore"])
        self.assertEqual(r["stop_reason"], "duration elapsed")
        self.assertTrue(all(s["ok"] for s in r["baseline"]))
        self.assertTrue(r["during"] and not any(s["ok"] for s in r["during"]))
        self.assertTrue(r["recovered"])

    def test_safety_hook_stops_early_and_restores(self):
        f = FakeFault()
        r = run({**EXP, "duration_s": 30}, POLICY, fault=f, probe_fn=probe_for(f),
                should_stop=lambda s, b, p: None if s["ok"] else "error rate")
        self.assertEqual(r["stop_reason"], "safety: error rate")
        self.assertEqual(len(r["during"]), 1)
        self.assertEqual(f.calls, ["inject", "restore"])

    def test_hard_timeout_stops_and_restores(self):
        f = FakeFault()
        r = run({**EXP, "duration_s": 30}, {**POLICY, "hard_timeout_s": 0.1}, fault=f, probe_fn=probe_for(f),
                should_stop=NEVER_STOP)
        self.assertEqual(r["stop_reason"], "hard timeout")
        self.assertEqual(f.calls, ["inject", "restore"])

    def test_docker_calls_have_timeout(self):
        with mock.patch.object(faults.subprocess, "run") as m:
            faults.ContainerStop().inject("web")
        self.assertEqual(m.call_args.kwargs["timeout"], faults.DOCKER_TIMEOUT_S)

    def test_hung_inject_still_restores(self):
        f = FakeFault()

        def hang(target):
            raise subprocess.TimeoutExpired("docker stop", faults.DOCKER_TIMEOUT_S)

        f.inject = hang
        with self.assertRaises(subprocess.TimeoutExpired):
            run(EXP, POLICY, fault=f, probe_fn=probe_for(f), should_stop=NEVER_STOP)
        self.assertEqual(f.calls, ["restore"])

    def test_kill_switch_restores(self):
        f = FakeFault()
        base = probe_for(f)

        def probe(url):
            if f.down:
                raise KeyboardInterrupt
            return base(url)

        r = run({**EXP, "duration_s": 30}, POLICY, fault=f, probe_fn=probe)
        self.assertEqual(r["stop_reason"], "kill switch (Ctrl+C)")
        self.assertEqual(f.calls, ["inject", "restore"])
        self.assertTrue(r["recovered"])

    def test_not_recovered_when_app_stays_down(self):
        f = FakeFault()
        r = run(EXP, {**POLICY, "recovery_timeout_s": 0.1}, fault=f,
                probe_fn=lambda url: {"t": 0, "ok": False, "status": None, "latency_ms": 1.0})
        self.assertFalse(r["recovered"])
        self.assertIsNone(r["recovery_s"])

    def test_network_delay_params(self):
        exp = {"delay_ms": 250, "jitter_ms": 25, "duration_s": 15}
        f = faults.NetworkDelay(exp)
        self.assertEqual(f.delay_ms, 250)
        self.assertEqual(f.jitter_ms, 25)
        self.assertEqual(f.duration_s, 15)

    def test_network_delay_inject_calls_docker(self):
        exp = {"delay_ms": 150, "jitter_ms": 15, "duration_s": 10}
        f = faults.NetworkDelay(exp)
        with mock.patch.object(faults.subprocess, "run") as m:
            f.inject("web")
        # Ensure timeout was passed
        self.assertEqual(m.call_args.kwargs["timeout"], faults.DOCKER_TIMEOUT_S)
        cmd = m.call_args.args[0]
        self.assertEqual(cmd[0], "docker")
        self.assertIn("pumba_delay_web", cmd)
        self.assertIn(faults.PUMBA_IMAGE, cmd)
        self.assertIn(faults.TC_IMAGE, cmd)
        self.assertIn("150", cmd)
        self.assertIn("15", cmd)

    def test_network_delay_restore_stops_gracefully(self):
        f = faults.NetworkDelay()
        with mock.patch.object(faults.subprocess, "run") as m:
            f.restore("web")
        # Should stop first, then rm
        calls = [c.args[0] for c in m.call_args_list]
        self.assertTrue(any(c[1] == "stop" and "pumba_delay_web" in c for c in calls))
        self.assertTrue(any(c[1] == "rm" and "pumba_delay_web" in c for c in calls))


if __name__ == "__main__":
    unittest.main()
