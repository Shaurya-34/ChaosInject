import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from chaos import controller, faults, hooks
from chaos.controller import PolicyError, run

# These tests check controller mechanics, not safety rules, so the safety hook never stops.
NEVER_STOP = lambda sample, baseline, policy: None
ALWAYS_DOWN = lambda url: {"t": 0, "ok": False, "status": None, "latency_ms": 1.0}

POLICY = {"allowed_targets": ["web"], "allowed_faults": ["container_stop"],
          "max_duration_s": 60, "hard_timeout_s": 120, "recovery_timeout_s": 1}
EXP = {"name": "t", "target": "web", "fault": "container_stop", "duration_s": 0.2,
       "probe_url": "http://x", "baseline_samples": 3, "interval_s": 0.01}


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

    def test_live_log_shows_each_phase_and_safety_stop(self):
        f = FakeFault()
        lines = []
        run({**EXP, "duration_s": 30}, POLICY, fault=f, probe_fn=probe_for(f),
            should_stop=lambda s, b, p: None if s["ok"] else "error rate", log=lines.append)
        text = "\n".join(lines)
        self.assertEqual(text.count("[baseline"), EXP["baseline_samples"])
        self.assertIn("injecting container_stop into web", text)
        self.assertIn("[fault   ] FAIL", text)
        self.assertIn("SAFETY STOP: error rate", text)
        self.assertIn("restored web", text)
        self.assertIn("[recovery] ok", text)

    def test_marker_exists_during_fault_and_is_removed_after_restore(self):
        f = FakeFault()
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "results" / ".active_fault.json"
            seen = []
            f.inject = lambda target: (seen.append(json.loads(marker.read_text())), setattr(f, "down", True))
            run(EXP, POLICY, fault=f, probe_fn=probe_for(f), should_stop=NEVER_STOP, marker=marker)
            self.assertEqual(seen[0]["target"], "web")
            self.assertEqual(seen[0]["fault"], "container_stop")
            self.assertFalse(marker.exists())

    def test_marker_kept_when_restore_fails(self):
        f = FakeFault()

        def fail(target):
            raise RuntimeError("docker start failed")

        f.restore = fail
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / ".active_fault.json"
            with self.assertRaises(RuntimeError):
                run(EXP, POLICY, fault=f, probe_fn=probe_for(f), should_stop=NEVER_STOP, marker=marker)
            self.assertTrue(marker.exists())

    def test_stop_file_during_fault_stops_and_restores(self):
        f = FakeFault()
        with tempfile.TemporaryDirectory() as d:
            stop = Path(d) / ".stop_request"
            healthy = probe_for(f)

            def probe(url):
                if f.down:
                    stop.write_text("")  # operator asks to stop mid-fault
                return healthy(url)

            r = run({**EXP, "duration_s": 30}, POLICY, fault=f, probe_fn=probe,
                    should_stop=NEVER_STOP, stop_file=stop)
        self.assertEqual(r["stop_reason"], "kill switch (operator stop)")
        self.assertEqual(len(r["during"]), 1)
        self.assertEqual(f.calls, ["inject", "restore"])

    def test_stop_file_before_injection_refuses(self):
        f = FakeFault()
        with tempfile.TemporaryDirectory() as d:
            stop = Path(d) / ".stop_request"
            stop.write_text("")
            r = run(EXP, POLICY, fault=f, probe_fn=probe_for(f), should_stop=NEVER_STOP, stop_file=stop)
        self.assertEqual(r["stop_reason"], "refused: operator stop before injection")
        self.assertEqual(f.calls, [])

    def test_recover_abandoned_restores_allowlisted_target(self):
        restored = []

        class Fault:
            def restore(self, target):
                restored.append(target)

        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / ".active_fault.json"
            marker.write_text(json.dumps({"target": "web", "fault": "container_stop"}))
            got = controller.recover_abandoned(marker, POLICY, faults={"container_stop": Fault}, log=lambda m: None)
            self.assertEqual(got, "web")
            self.assertEqual(restored, ["web"])
            self.assertFalse(marker.exists())

    def test_recover_abandoned_refuses_non_allowlisted_target(self):
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / ".active_fault.json"
            marker.write_text(json.dumps({"target": "someone-elses-db", "fault": "container_stop"}))
            with self.assertRaises(PolicyError):
                controller.recover_abandoned(marker, POLICY, faults={"container_stop": FakeFault}, log=lambda m: None)
            self.assertTrue(marker.exists())

    def test_recover_abandoned_without_marker_does_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(controller.recover_abandoned(Path(d) / "none.json", POLICY, log=lambda m: None))

    def test_broken_log_never_blocks_restore(self):
        f = FakeFault()

        def broken(msg):
            raise BrokenPipeError("stdout closed")

        r = run(EXP, POLICY, fault=f, probe_fn=probe_for(f), should_stop=NEVER_STOP, log=broken)
        self.assertEqual(f.calls, ["inject", "restore"])
        self.assertTrue(r["recovered"])

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
        healthy = probe_for(f)
        r = run(EXP, {**POLICY, "recovery_timeout_s": 0.1}, fault=f, should_stop=NEVER_STOP,
                probe_fn=lambda url: ALWAYS_DOWN(url) if f.calls else healthy(url))
        self.assertEqual(f.calls, ["inject", "restore"])
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
        with mock.patch.object(faults.subprocess, "run") as m, \
             mock.patch.object(f, "_qdisc", return_value="qdisc netem 1: root delay 150ms"):
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

    def test_network_delay_inject_netem_never_appears_raises(self):
        f = faults.NetworkDelay()
        times = [0.0, 25.0]
        with mock.patch.object(faults.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="")), \
             mock.patch.object(f, "_qdisc", return_value="qdisc noqueue 0: root"), \
             mock.patch.object(faults.time, "monotonic", side_effect=lambda: times.pop(0) if times else 30.0), \
             mock.patch.object(faults.time, "sleep"):
            with self.assertRaises(faults.InjectError):
                f.inject("web")

    def test_network_delay_restore_stops_gracefully(self):
        f = faults.NetworkDelay()
        with mock.patch.object(faults.subprocess, "run") as m:
            m.return_value = mock.Mock(stdout="qdisc noqueue 0: dev eth0 root", returncode=0)
            f.restore("web")
        calls = [c.args[0] for c in m.call_args_list]
        self.assertTrue(any(c[1] == "stop" and "pumba_delay_web" in c for c in calls))
        self.assertTrue(any(c[1] == "rm" and "pumba_delay_web" in c for c in calls))

    def test_network_delay_restore_removes_lingering_netem(self):
        f = faults.NetworkDelay()
        # First check returns netem, second check returns clean
        check1 = mock.Mock(stdout="qdisc netem 1: root refcnt 2 delay 200.0ms", returncode=0)
        check2 = mock.Mock(stdout="qdisc pfifo_fast 0: root", returncode=0)
        del_call = mock.Mock(stdout="", returncode=0)
        generic_ok = mock.Mock(stdout="", returncode=0)

        def mock_run(cmd, *args, **kwargs):
            if "tc" in cmd and "show" in cmd:
                if not getattr(mock_run, "called_del", False):
                    return check1
                return check2
            if "tc" in cmd and "del" in cmd:
                mock_run.called_del = True
                return del_call
            return generic_ok

        with mock.patch.object(faults.subprocess, "run", side_effect=mock_run):
            f.restore("web")
        self.assertTrue(getattr(mock_run, "called_del", False))

    def test_network_delay_restore_removal_fails_raises(self):
        f = faults.NetworkDelay()
        # Check returns netem, and tc del raises
        check = mock.Mock(stdout="qdisc netem 1: root refcnt 2 delay 200.0ms", returncode=0)

        def mock_run(cmd, *args, **kwargs):
            if "tc" in cmd and "show" in cmd:
                return check
            if "tc" in cmd and "del" in cmd:
                raise subprocess.CalledProcessError(1, cmd, stderr="RTNETLINK answers: No such file")
            return mock.Mock(stdout="", returncode=0)

        with mock.patch.object(faults.subprocess, "run", side_effect=mock_run):
            with self.assertRaises(faults.CleanupError):
                f.restore("web")

    def test_network_delay_restore_verification_fails_raises(self):
        f = faults.NetworkDelay()
        # Verification command itself fails (e.g. image pull error, target not running)
        failed = mock.Mock(stdout="", stderr="Error: No such container", returncode=1)

        def mock_run(cmd, *args, **kwargs):
            if "tc" in cmd and "show" in cmd:
                return failed
            return mock.Mock(stdout="", returncode=0)

        with mock.patch.object(faults.subprocess, "run", side_effect=mock_run):
            with self.assertRaises(faults.CleanupError):
                f.restore("web")


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class RunLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(hooks, "RESULTS_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_pid_alive(self):
        self.assertTrue(controller.pid_alive(os.getpid()))
        self.assertFalse(controller.pid_alive(dead_pid()))

    def test_acquire_and_release(self):
        controller.acquire_lock("exp.json")
        self.assertEqual(controller.read_lock()["pid"], os.getpid())
        controller.release_lock()
        self.assertFalse(controller.lock_path().exists())

    def test_live_lock_refuses_second_run(self):
        controller.lock_path().write_text(json.dumps({"pid": os.getpid(), "experiment": "first"}))
        with self.assertRaisesRegex(PolicyError, "another run is in progress"):
            controller.acquire_lock("second")

    def test_stale_lock_is_taken_over(self):
        controller.lock_path().write_text(json.dumps({"pid": dead_pid(), "experiment": "old"}))
        self.assertIsNone(controller.read_lock())
        controller.acquire_lock("new")
        self.assertEqual(controller.read_lock()["experiment"], "new")

    def test_release_leaves_someone_elses_lock(self):
        controller.lock_path().write_text(json.dumps({"pid": os.getpid() + 1}))
        controller.release_lock()
        self.assertTrue(controller.lock_path().exists())

    def test_main_refused_while_locked_keeps_stop_request(self):
        controller.lock_path().write_text(json.dumps({"pid": os.getpid(), "experiment": "first"}))
        controller.stop_path().write_text("")
        with mock.patch.object(controller, "run") as run_mock, self.assertRaises(SystemExit) as exit_:
            controller.main(["controller", "exp.json"])
        self.assertIn("another run is in progress", str(exit_.exception))
        run_mock.assert_not_called()
        self.assertTrue(controller.stop_path().exists())  # the first run's stop request survives


class BaselineCheckTest(unittest.TestCase):
    def test_unhealthy_baseline_refuses_without_injecting(self):
        f = FakeFault()
        r = run(EXP, POLICY, fault=f, probe_fn=ALWAYS_DOWN, should_stop=NEVER_STOP)
        self.assertEqual(f.calls, [])  # never injected, so nothing to restore
        self.assertTrue(r["refused"])
        self.assertTrue(r["stop_reason"].startswith("refused: baseline already unhealthy"))
        self.assertEqual(len(r["baseline"]), EXP["baseline_samples"])
        self.assertEqual((r["during"], r["recovery"]), ([], []))
        self.assertIsNone(r["fault_started"])

    def test_check_baseline_reason_is_recorded(self):
        f = FakeFault()
        r = run(EXP, POLICY, fault=f, probe_fn=probe_for(f), should_stop=NEVER_STOP,
                check_baseline=lambda baseline, policy: "too slow")
        self.assertEqual(r["stop_reason"], "refused: too slow")
        self.assertEqual(f.calls, [])

    def test_check_baseline_sees_baseline_and_policy(self):
        seen = {}

        def spy(baseline, policy):
            seen["n"], seen["policy"] = len(baseline), policy
            return None

        f = FakeFault()
        r = run(EXP, POLICY, fault=f, probe_fn=probe_for(f), should_stop=NEVER_STOP, check_baseline=spy)
        self.assertEqual(seen, {"n": EXP["baseline_samples"], "policy": POLICY})
        self.assertEqual(f.calls, ["inject", "restore"])
        self.assertNotIn("refused", r)

    def test_too_few_baseline_samples_refuses(self):
        f = FakeFault()
        r = run({**EXP, "baseline_samples": 1}, POLICY, fault=f, probe_fn=probe_for(f))
        self.assertIn("baseline too short", r["stop_reason"])
        self.assertEqual(f.calls, [])

    def test_reset_state_called_before_first_probe(self):
        order = []
        f = FakeFault()
        healthy = probe_for(f)

        def probe(url):
            order.append("probe")
            return healthy(url)

        with mock.patch.object(hooks, "reset_state", side_effect=lambda: order.append("reset")):
            run(EXP, POLICY, fault=f, probe_fn=probe, should_stop=NEVER_STOP)
        self.assertEqual(order[0], "reset")
        self.assertEqual(order.count("reset"), 1)

    def test_main_saves_refused_run(self):
        refused = {"stop_reason": "refused: baseline already unhealthy", "refused": True,
                   "baseline": [], "during": [], "recovery": [], "recovered": False, "recovery_s": None}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(hooks, "RESULTS_DIR", Path(d)), \
                mock.patch.object(controller, "run", return_value=refused), \
                mock.patch.object(hooks, "save_result", return_value="results/x.json") as save, \
                mock.patch.object(controller.Path, "read_text", return_value="{}"), \
                mock.patch("builtins.print") as out:
            controller.main(["controller", "exp.json"])
        save.assert_called_once_with(refused)
        printed = " ".join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn("no fault injected", printed)
        self.assertIn("results/x.json", printed)


if __name__ == "__main__":
    unittest.main()

