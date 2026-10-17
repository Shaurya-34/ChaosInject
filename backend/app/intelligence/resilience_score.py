"""Resilience score: a deterministic 0-100 number computed from saved chaos runs only.

The formula, its weights and worked examples are documented in ``docs/resilience-score.md``. In short:

* **Run score R** = 100 x (0.40 recovered + 0.25 error + 0.20 latency + 0.15 recovery time), each part 0..1.
  A run that did not recover scores 0. A run that ended in a safety stop (``stop_reason`` starts with
  ``safety:``) is capped at 50. Refused runs, and runs with no fault-phase data, are not scored.
* **Per (target, fault):** the latest scored run counts; every run id stays listed.
* **Per target:** 0.5 x mean + 0.5 x min of its pair scores.
* **Overall:** mean of the mapped target scores weighted by criticality (floor 0.1).
* **Coverage** (tested pairs of possible pairs) is reported next to the score and never multiplied in.
  With no scored run the score is ``None``.

Pure functions: no I/O, no model, no randomness. Every number in the output is copied from a run record
or computed from those copies, and the output names the ``run_id`` it came from.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .injection_agent import FAULT_TEMPLATES, criticality, node_for_target

FORMULA_VERSION = 1
WEIGHTS = {"recovered": 0.40, "error": 0.25, "latency": 0.20, "recovery_time": 0.15}
SAFETY_STOP_CAP = 50.0
FAST_RECOVERY_S = 2.0            # recovery within this many seconds earns the full recovery-time part
DEFAULT_RECOVERY_TIMEOUT_S = 30.0  # policy.recovery_timeout_s: recovery at or beyond this earns 0
CRITICALITY_FLOOR = 0.1


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _phase(run: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    return (run.get("summary") or {}).get(name) or {}


def _is_safety_stop(run: Mapping[str, Any]) -> bool:
    return str(run.get("stop_reason") or "").startswith("safety:")


def unscored_reason(run: Mapping[str, Any]) -> str | None:
    """Why a run cannot be scored, or ``None`` if it can."""
    exp = run.get("experiment") or {}
    if not exp.get("target") or not exp.get("fault"):
        return "no target or fault in the record"
    if run.get("refused"):
        return "refused before injecting; nothing was learned about the system"
    if _phase(run, "during").get("error_rate") is None:
        return "no fault-phase probes were recorded"
    if not isinstance(run.get("recovered"), bool):
        return "the record has no recovered flag"
    return None


def score_run(run: Mapping[str, Any], recovery_timeout_s: float = DEFAULT_RECOVERY_TIMEOUT_S) -> dict[str, Any]:
    """Score one scorable run. ``unscored_reason(run)`` must be ``None``."""

    during, baseline = _phase(run, "during"), _phase(run, "baseline")
    error_rate = during["error_rate"]
    base_p95, fault_p95 = baseline.get("p95_ms"), during.get("p95_ms")
    recovered, recovery_s = run["recovered"], run.get("recovery_s")

    parts: dict[str, float | None] = {
        "recovered": 1.0 if recovered else 0.0,
        "error": _clamp01(1.0 - error_rate),
        "latency": None,
        "recovery_time": None,
    }
    if fault_p95 is None:  # every fault-phase probe failed, so there is no latency to speak of
        parts["latency"] = 0.0
    elif base_p95:
        parts["latency"] = 1.0 / max(1.0, fault_p95 / base_p95)
    # else: no baseline p95 to compare with; the part is left out and the others are renormalised
    if not recovered:
        parts["recovery_time"] = 0.0
    elif recovery_s is not None:
        span = max(recovery_timeout_s - FAST_RECOVERY_S, 1e-9)
        parts["recovery_time"] = 1.0 if recovery_s <= FAST_RECOVERY_S else _clamp01(1.0 - (recovery_s - FAST_RECOVERY_S) / span)

    included = {k: v for k, v in parts.items() if v is not None}
    total_weight = sum(WEIGHTS[k] for k in included)
    uncapped = 100.0 * sum(WEIGHTS[k] * v for k, v in included.items()) / total_weight
    if not recovered:
        uncapped = 0.0
    capped = _is_safety_stop(run) and uncapped > SAFETY_STOP_CAP
    score = SAFETY_STOP_CAP if capped else uncapped
    return {
        "run_id": run["run_id"],
        "score": round(score, 1),
        "uncapped_score": round(uncapped, 1),
        "capped": capped,
        "cap_reason": "safety stop" if capped else None,
        "not_recovered": not recovered,
        "components": {
            k: {"value": None if parts[k] is None else round(parts[k], 4), "weight": WEIGHTS[k],
                "included": parts[k] is not None}
            for k in WEIGHTS
        },
        "inputs": {
            "recovered": recovered,
            "error_rate": error_rate,
            "baseline_p95_ms": base_p95,
            "during_p95_ms": fault_p95,
            "recovery_s": recovery_s,
            "recovery_timeout_s": recovery_timeout_s,
            "stop_reason": run.get("stop_reason"),
        },
    }


def _latest(runs: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    return max(runs, key=lambda r: (r.get("saved_at") or 0, r["run_id"]))


def possible_pairs(dep_map: Mapping[str, Any], policy: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(target container, fault) pairs that exist in the dependency map and that the policy allows."""
    nodes = {n["id"] for n in dep_map.get("nodes", [])}
    faults = [f for f in policy.get("allowed_faults", []) if f in FAULT_TEMPLATES]
    return sorted(
        (t, f) for t in policy.get("allowed_targets", []) if node_for_target(t) in nodes for f in faults
    )


def compute(
    runs: Sequence[Mapping[str, Any]], dep_map: Mapping[str, Any], policy: Mapping[str, Any]
) -> dict[str, Any]:
    timeout = float(policy.get("recovery_timeout_s", DEFAULT_RECOVERY_TIMEOUT_S))
    nodes = {n["id"] for n in dep_map.get("nodes", [])}
    not_scored: list[dict[str, str]] = []
    by_pair: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for run in runs:
        reason = unscored_reason(run)
        if reason:
            not_scored.append({"run_id": run.get("run_id", "?"), "reason": reason})
            continue
        exp = run["experiment"]
        by_pair.setdefault((exp["target"], exp["fault"]), []).append(run)

    by_target: dict[str, list[dict[str, Any]]] = {}
    for (target, fault), group in sorted(by_pair.items()):
        chosen = _latest(group)
        scored = score_run(chosen, timeout)
        by_target.setdefault(target, []).append({
            "fault": fault,
            "score": scored["score"],
            "scored_run_id": chosen["run_id"],
            "run_ids": [r["run_id"] for r in sorted(group, key=lambda r: (r.get("saved_at") or 0, r["run_id"]))],
            **{k: scored[k] for k in ("uncapped_score", "capped", "cap_reason", "not_recovered", "components", "inputs")},
        })

    targets, unmapped = [], []
    for target, pairs in sorted(by_target.items()):
        scores = [p["score"] for p in pairs]
        target_score = round(0.5 * (sum(scores) / len(scores)) + 0.5 * min(scores), 1)
        node = node_for_target(target)
        entry = {"target": target, "node": node, "score": target_score, "pairs": pairs}
        if node in nodes:
            crit = criticality(dep_map, node)
            entry.update(criticality=crit, weight=round(max(CRITICALITY_FLOOR, crit), 4))
            targets.append(entry)
        else:
            unmapped.append(entry)  # e.g. the dummy test target: shown, but not part of the overall score

    overall = None
    if targets:
        total_w = sum(t["weight"] for t in targets)
        overall = round(sum(t["score"] * t["weight"] for t in targets) / total_w, 1)

    possible = possible_pairs(dep_map, policy)
    tested = {(t["target"], p["fault"]) for t in targets for p in t["pairs"]}
    untested = [{"target": t, "fault": f} for t, f in possible if (t, f) not in tested]
    n_tested = len(tested & set(possible))
    coverage = {
        "tested": n_tested,
        "possible": len(possible),
        "fraction": round(n_tested / len(possible), 4) if possible else None,
        "untested": untested,
    }
    complete = bool(possible) and not untested
    if overall is None:
        headline = "n/a (no scored runs yet)"
    else:
        headline = f"{overall:g}" + ("" if complete else f" (partial: {n_tested} of {len(possible)} pairs tested)")
    return {
        "formula_version": FORMULA_VERSION,
        "score": overall,
        "headline": headline,
        "complete": complete,
        "coverage": coverage,
        "weights": dict(WEIGHTS),
        "safety_stop_cap": SAFETY_STOP_CAP,
        "targets": targets,
        "unmapped_targets": unmapped,
        "runs_not_scored": not_scored,
    }
