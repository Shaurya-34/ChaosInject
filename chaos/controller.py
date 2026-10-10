"""Experiment controller: baseline -> inject -> observe -> restore -> recovery.

Usage: python -m chaos.controller experiments/dummy_web_stop.json
Kill switch: Ctrl+C. The fault is always restored, even on error or interrupt.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from chaos import hooks
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


def run(exp, policy, fault=None, probe_fn=probe, should_stop=hooks.should_stop, sleep=time.sleep):
    check_policy(exp, policy)
    fault = fault or FAULTS[exp["fault"]](exp)
    url, interval = exp["probe_url"], exp["interval_s"]
    deadline = time.monotonic() + policy["hard_timeout_s"]
    record = {"experiment": exp, "policy": policy, "baseline": [], "during": [], "recovery": [],
              "stop_reason": None, "recovered": False, "recovery_s": None}

    for _ in range(exp["baseline_samples"]):
        record["baseline"].append(probe_fn(url))
        sleep(interval)

    record["fault_started"] = time.time()
    try:
        fault.inject(exp["target"])
        end = time.monotonic() + exp["duration_s"]
        while time.monotonic() < end:
            if time.monotonic() > deadline:
                record["stop_reason"] = "hard timeout"
                break
            sample = probe_fn(url)
            record["during"].append(sample)
            reason = should_stop(sample, record["baseline"], policy)
            if reason:
                record["stop_reason"] = f"safety: {reason}"
                break
            sleep(interval)
        else:
            record["stop_reason"] = "duration elapsed"
    except KeyboardInterrupt:
        record["stop_reason"] = "kill switch (Ctrl+C)"
    finally:
        fault.restore(exp["target"])
        record["fault_ended"] = time.time()

    restored_at = time.monotonic()
    recovery_end = min(deadline, restored_at + policy["recovery_timeout_s"])
    while time.monotonic() < recovery_end:
        sample = probe_fn(url)
        record["recovery"].append(sample)
        if sample["ok"]:
            record["recovered"] = True
            record["recovery_s"] = round(time.monotonic() - restored_at, 2)
            break
        sleep(interval)

    return record


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__)
    exp = json.loads(Path(argv[1]).read_text())
    policy = json.loads(POLICY_PATH.read_text())
    try:
        record = run(exp, policy)
    except PolicyError as e:
        sys.exit(f"refused: {e}")
    path = hooks.save_result(record)
    errors = sum(not s["ok"] for s in record["during"])
    print(f"stop reason: {record['stop_reason']}")
    print(f"during fault: {errors}/{len(record['during'])} probes failed")
    print(f"recovered: {record['recovered']} in {record['recovery_s']}s")
    print(f"saved: {path}")


if __name__ == "__main__":
    main(sys.argv)
