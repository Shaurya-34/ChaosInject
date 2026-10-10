import time
import unittest

from chaos.controller import PolicyError, run

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
        r = run(EXP, POLICY, fault=f, probe_fn=probe_for(f))
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
        r = run({**EXP, "duration_s": 30}, {**POLICY, "hard_timeout_s": 0.1}, fault=f, probe_fn=probe_for(f))
        self.assertEqual(r["stop_reason"], "hard timeout")
        self.assertEqual(f.calls, ["inject", "restore"])

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


if __name__ == "__main__":
    unittest.main()
