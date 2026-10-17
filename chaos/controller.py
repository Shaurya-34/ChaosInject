"""Experiment controller: baseline -> inject -> observe -> restore -> recovery.

Usage: python -m chaos.controller experiments/dummy_web_stop.json
       python -m chaos.controller --recover   (restore a fault left by a killed run)
Kill switch: Ctrl+C, or create results/.stop_request (used by the API). The fault is always
restored, even on error or interrupt.
If the process itself is killed, the next run (or --recover) restores the leftover fault first.
Only one controller runs at a time: results/.active_run.json holds the running controller's pid.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from chaos import hooks, metrics
from chaos.faults import FAULTS

POLICY_PATH = Path(__file__).resolve().parent / "policy.json"


class PolicyError(Exception):
    pass


def probe(url, timeout=2.0):
    start = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    except (urllib.error.URLError, OSError):
        status = None
    latency_ms = (time.monotonic() - start) * 1000
    ok = status is not None and status < 500
    return {"t": time.time(), "ok": ok, "status": status, "latency_ms": round(latency_ms, 1)}


def check_policy(exp, policy):
    if exp["target"] not in policy["allowed_targets"]:
        raise PolicyError(f"target {exp['target']!r} is not allowlisted")
    if exp["fault"] not in policy["allowed_faults"]:
        raise PolicyError(f"fault {exp['fault']!r} is not allowlisted")
    if exp["fault"] not in FAULTS:
        raise PolicyError(f"fault {exp['fault']!r} is not implemented")
    if exp["duration_s"] > policy["max_duration_s"]:
        raise PolicyError(f"duration {exp['duration_s']}s exceeds max {policy['max_duration_s']}s")


def _fmt(phase, s):
    status = s["status"] if s["status"] is not None else "---"
    return f"  [{phase:<8}] {'ok  ' if s['ok'] else 'FAIL'} status={status} {s['latency_ms']:>7.1f}ms"


def marker_path():
    """Exists only while a fault is injected; a leftover one means a run was killed mid-fault."""
    return hooks.RESULTS_DIR / ".active_fault.json"


def stop_path():
    """Creating this file asks a running controller to stop and restore (a kill switch without Ctrl+C)."""
    return hooks.RESULTS_DIR / ".stop_request"


def lock_path():
    """Exists while a controller process runs (any phase); holds its pid."""
    return hooks.RESULTS_DIR / ".active_run.json"


def pid_alive(pid):
    if os.name == "nt":  # os.kill(pid, 0) would terminate the process on Windows
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: exists, owned by someone else
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_lock():
    """The live lock's contents, or None if no controller is running (a stale lock counts as none)."""
    try:
        info = json.loads(lock_path().read_text())
    except (FileNotFoundError, ValueError):
        return None
    return info if isinstance(info.get("pid"), int) and pid_alive(info["pid"]) else None


def acquire_lock(experiment):
    """Claim the run lock for this process, or raise PolicyError if another controller holds it."""
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    info = {"pid": os.getpid(), "experiment": experiment, "started_at": time.time()}
    for _ in range(2):
        try:
            with open(path, "x") as fh:  # atomic: fails if the file exists
                fh.write(json.dumps(info))
            return
        except FileExistsError:
            held = read_lock()
            if held:
                raise PolicyError(f"another run is in progress ({held.get('experiment')}, pid {held['pid']})")
            path.unlink(missing_ok=True)  # stale lock from a dead controller
    raise PolicyError("could not acquire the run lock")


def release_lock():
    try:
        if json.loads(lock_path().read_text()).get("pid") == os.getpid():
            lock_path().unlink()
    except (FileNotFoundError, ValueError):
        pass


def recover_abandoned(marker, policy, faults=FAULTS, log=print):
    """Restore a fault left behind by a killed run. Returns the restored target, or None."""
    if not marker.exists():
        return None
    info = json.loads(marker.read_text())
    target, name = info["target"], info["fault"]
    if target not in policy["allowed_targets"] or name not in faults:
        raise PolicyError(f"leftover fault marker {marker} names {name!r} on {target!r}, which is not "
                          "allowlisted; restore it by hand, then delete the marker")
    log(f"found leftover {name} on {target} from a killed run; restoring it first")
    faults[name]().restore(target)
    marker.unlink()
    return target


def run(exp, policy, fault=None, probe_fn=probe, should_stop=hooks.should_stop, sleep=time.sleep,
        check_baseline=hooks.check_baseline, log=lambda msg: None, marker=None, stop_file=None,
        metrics=None):
    check_policy(exp, policy)

    def say(msg):
        try:
            log(msg)
        except Exception:
            pass  # output problems (e.g. a closed pipe) must never block a restore

    def note(method, *args):
        if metrics is not None:
            try:
                getattr(metrics, method)(*args)
            except Exception:
                pass  # metrics are observability only; they must never affect a run

    hooks.reset_state()  # fresh safety window for every run
    fault = fault or FAULTS[exp["fault"]](exp)
    url, interval = exp["probe_url"], exp["interval_s"]
    deadline = time.monotonic() + policy["hard_timeout_s"]
    record = {"experiment": exp, "policy": policy, "baseline": [], "during": [], "recovery": [],
              "stop_reason": None, "recovered": False, "recovery_s": None}

    for _ in range(exp["baseline_samples"]):
        record["baseline"].append(probe_fn(url))
        say(_fmt("baseline", record["baseline"][-1]))
        note("sample", "baseline", record["baseline"][-1])
        sleep(interval)

    # Pre-flight: never inject into an app that is already unhealthy.
    reason = check_baseline(record["baseline"], policy)
    if not reason and stop_file and stop_file.exists():
        reason = "operator stop before injection"
    if reason:
        record["stop_reason"] = f"refused: {reason}"
        record["refused"] = True
        record["fault_started"] = record["fault_ended"] = None
        return record

    record["fault_started"] = time.time()
    if marker:  # written before injecting; if this fails, nothing is injected
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"target": exp["target"], "fault": exp["fault"],
                                      "experiment": exp["name"], "started": record["fault_started"]}))
    try:
        say(f"injecting {exp['fault']} into {exp['target']} for up to {exp['duration_s']}s")
        fault.inject(exp["target"])
        note("set_fault", True)
        end = time.monotonic() + exp["duration_s"]
        while time.monotonic() < end:
            if time.monotonic() > deadline:
                record["stop_reason"] = "hard timeout"
                break
            if stop_file and stop_file.exists():
                record["stop_reason"] = "kill switch (operator stop)"
                say("KILL SWITCH: operator stop")
                break
            sample = probe_fn(url)
            record["during"].append(sample)
            say(_fmt("fault", sample))
            note("sample", "fault", sample)
            reason = should_stop(sample, record["baseline"], policy)
            if reason:
                record["stop_reason"] = f"safety: {reason}"
                say(f"SAFETY STOP: {reason}")
                break
            sleep(interval)
        else:
            record["stop_reason"] = "duration elapsed"
    except KeyboardInterrupt:
        record["stop_reason"] = "kill switch (Ctrl+C)"
        say("KILL SWITCH: Ctrl+C")
    finally:
        fault.restore(exp["target"])  # restore first: nothing may run before it
        if marker:
            marker.unlink(missing_ok=True)  # only reached if restore succeeded
        record["fault_ended"] = time.time()
        say(f"restored {exp['target']}")
        note("set_fault", False)

    restored_at = time.monotonic()
    recovery_end = min(deadline, restored_at + policy["recovery_timeout_s"])
    while time.monotonic() < recovery_end:
        sample = probe_fn(url)
        record["recovery"].append(sample)
        say(_fmt("recovery", sample))
        note("sample", "recovery", sample)
        if sample["ok"]:
            record["recovered"] = True
            record["recovery_s"] = round(time.monotonic() - restored_at, 2)
            break
        sleep(interval)

    return record


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__)
    policy = json.loads(POLICY_PATH.read_text())
    try:
        acquire_lock("--recover" if argv[1] == "--recover" else argv[1])
    except PolicyError as e:
        sys.exit(f"refused: {e}")
    try:
        _main_locked(argv, policy)
    finally:
        release_lock()


def _main_locked(argv, policy):
    marker = marker_path()
    if argv[1] == "--recover":
        try:
            target = recover_abandoned(marker, policy)
        except PolicyError as e:
            sys.exit(f"refused: {e}")
        print("nothing to recover" if target is None else f"restored {target}")
        return
    exp = json.loads(Path(argv[1]).read_text())
    stop_file = stop_path()
    stop_file.unlink(missing_ok=True)  # a stale request must not stop this new run
    run_metrics = metrics.Metrics(exp)
    gateway = os.environ.get("CHAOS_PUSHGATEWAY", "http://127.0.0.1:9091")  # empty disables
    pusher = metrics.Pusher(run_metrics, gateway).start() if gateway else None
    record = None
    try:
        recover_abandoned(marker, policy)
        record = run(exp, policy, log=lambda msg: print(msg, flush=True), marker=marker,
                     stop_file=stop_file, metrics=run_metrics)
    except PolicyError as e:
        sys.exit(f"refused: {e}")
    finally:
        if pusher and record is None:  # refused or crashed: clear this run from the gateway
            pusher.stop()
    path = hooks.save_result(record)
    run_metrics.finish(record)
    if pusher:
        pusher.stop(linger=float(os.environ.get("CHAOS_METRICS_LINGER_S", "3")) if pusher.pushes else 0)
    print(f"stop reason: {record['stop_reason']}")
    if record.get("refused"):
        print("no fault injected")
        print(f"saved: {path}")
        return
    errors = sum(not s["ok"] for s in record["during"])
    print(f"during fault: {errors}/{len(record['during'])} probes failed")
    print(f"recovered: {record['recovered']} in {record['recovery_s']}s")
    print(f"saved: {path}")


if __name__ == "__main__":
    main(sys.argv)
