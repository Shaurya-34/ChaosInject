# Resilience score

A single 0-100 number for "how well does the system survive the faults we have injected", computed **only from saved runs** (`results/<run_id>.json`). It is deterministic: the same runs always give the same score. No model is involved, and every number in the output names the `run_id` it came from.

Code: `backend/app/intelligence/resilience_score.py`. API: `GET /api/v1/chaos/score` (also embedded as `score` in `GET /api/v1/chaos/findings` and in the findings report).

## 1. Run score

For one run:

```text
R = 100 x ( 0.40 x recovered + 0.25 x error + 0.20 x latency + 0.15 x recovery_time )
```

Each part is between 0 and 1 and is read from the run record (field names as in `contracts/run.schema.json`):

| Part | Weight | Value | Record field |
|---|---|---|---|
| `recovered` | 0.40 | 1 if the system recovered after the fault was removed, else 0 | `recovered` |
| `error` | 0.25 | 1 - error rate during the fault | `summary.during.error_rate` |
| `latency` | 0.20 | 1 / max(1, fault p95 / baseline p95): 1.0 with no inflation, 0.5 at 2x, 0.2 at 5x. If every fault-phase probe failed (no p95) it is 0. | `summary.during.p95_ms`, `summary.baseline.p95_ms` |
| `recovery_time` | 0.15 | 1 if recovery took 2 s or less, falling linearly to 0 at the policy timeout (30 s) | `recovery_s`, `chaos/policy.json` `recovery_timeout_s` |

If a part cannot be computed (no baseline p95, or recovered but no `recovery_s`), it is left out and the remaining weights are renormalised, so a missing measurement never counts as a failure.

Two rules apply on top of the formula:

- **Not recovered scores 0.** A run with `recovered: false` is 0 regardless of its other parts.
- **Safety-stop cap.** If the run ended in a safety stop (`stop_reason` starts with `safety:`), its score is capped at **50**. A run that breached the safety thresholds must not look healthy even if the system came back quickly. Lower scores are not raised, and other stops (`kill switch`, `hard timeout`, `duration elapsed`) are not capped. The breakdown shows `capped: safety stop` together with the uncapped score.

Runs that were refused before injecting, runs with no fault-phase probes and runs without a `recovered` flag are **not scored**. They are listed under `runs_not_scored` with the reason.

### Worked examples

| Run | Parts | Uncapped | Result |
|---|---|---|---|
| geo `container_stop`, every probe failed (no p95), recovered in 1.2 s | 1, 0, 0, 1 | 100 x (0.40 + 0.15) = **55** | 55 if it ran to the end; **50** when a safety rule stopped it (the usual case) |
| rate `network_delay`, 0% errors, p95 40 ms to 1500 ms (37.5x), recovered in 0.5 s | 1, 1, 0.027, 1 | 100 x (0.40 + 0.25 + 0.20 x 0.027 + 0.15) = **80.5** | 80.5; **50** if the safety stop fired |
| any run that did not recover | | | **0** |

## 2. From runs to one number

1. **Per (target, fault) pair:** the **latest** scored run counts (by `saved_at`), because that reflects the system after any fix. Older runs stay listed in `run_ids`, and `scored_run_id` names the one used. This is what makes a before/after comparison work: re-run an experiment after a fix and the pair's score moves.
2. **Per target:** `0.5 x mean + 0.5 x min` of its pair scores. One catastrophic fault cannot hide behind good ones.
3. **Overall:** the mean of the target scores weighted by **criticality**, the share of working endpoints that break when the target fails, with a bonus for `/hotels` (the same `criticality()` the injection agent uses, from `docs/dependency-map.json`). Weights have a floor of 0.1 so no target counts for nothing. Targets that are not in the dependency map (for example the dummy test container) are shown under `unmapped_targets` but are not part of the overall score.

## 3. Coverage and the headline

The score only describes what has been tested. **Coverage** is the number of tested (target, fault) pairs out of the possible ones (dependency-map targets the policy allows, times the implemented faults the policy allows). It is reported next to the score and **never multiplied into it**:

- complete coverage: `72`
- partial: `72 (partial: 5 of 9 pairs tested)`
- no scored runs: `n/a (no scored runs yet)`, and the score is `null`, never 0.

The untested pairs are listed so the next experiment to run is obvious.

## 4. Output

```json
{
  "formula_version": 1, "score": 65.2, "headline": "65.2 (partial: 2 of 9 pairs tested)", "complete": false,
  "coverage": {"tested": 2, "possible": 9, "fraction": 0.2222, "untested": [{"target": "...", "fault": "..."}]},
  "weights": {"recovered": 0.4, "error": 0.25, "latency": 0.2, "recovery_time": 0.15}, "safety_stop_cap": 50,
  "targets": [{"target": "hotel-sandbox-geo-1", "node": "geo", "score": 50.0, "criticality": 0.45, "weight": 0.45,
    "pairs": [{"fault": "container_stop", "score": 50.0, "uncapped_score": 55.0, "capped": true, "cap_reason": "safety stop",
      "scored_run_id": "...", "run_ids": ["..."],
      "components": {"recovered": {"value": 1.0, "weight": 0.4, "included": true}},
      "inputs": {"recovered": true, "error_rate": 1.0, "baseline_p95_ms": 41.0, "during_p95_ms": null,
                 "recovery_s": 1.2, "recovery_timeout_s": 30, "stop_reason": "safety: ..."}}]}],
  "unmapped_targets": [], "runs_not_scored": []
}
```

## 5. Limits

- It measures only the faults and targets that have been run. A high score with low coverage means "what we tried held up", not "the system is resilient".
- Equal treatment of faults inside a target and the 0.40 / 0.25 / 0.20 / 0.15 weights are judgement calls, documented here so they can be challenged and changed in one place (`WEIGHTS`).
- Scores from different machines are comparable only if the probe URL, durations and policy are the same.
