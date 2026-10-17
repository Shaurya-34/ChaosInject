# Telemetry workstream (Navya)

> **Removed (gauthamop/remove-learner):** the learning channel (`learning.py`, `LearningObservation`, `LearningKind`, `LearningChannelConfig`) and the PostgreSQL plan estimator were removed. Sections below that describe them are historical.

Owner: Navya · Branch: `feat/navya-telemetry` · Context: `kind-bosch-navya`
Paths: `backend/app/telemetry/`, `tests/telemetry/`, `docs/telemetry/`, `scripts/telemetry/`

## Status — read this first

| Area | State |
|---|---|
| Sampling, histogram, bounded ring | Implemented, unit-tested |
| Phases and window alignment | Implemented, unit-tested |
| System state and freshness | Implemented, unit-tested |
| Recovery detection | Implemented, unit-tested |
| Resilience scoring | Implemented, unit-tested |
| Run summary, evidence hash, evidence refs | Implemented, unit-tested |
| Plan estimator | Implemented, tested against **synthetic plans only** |
| Learning channel | Implemented, unit-tested |
| Prevention acceptance verifier (LOCAL DEMO) | Implemented, tested over a real loopback socket |
| Synthetic probe | Implemented, tested against a **scripted transport only** |
| Live Online Boutique run | **Not done.** Needs Ananya's deployment and marker calibration |
| Live PostgreSQL EXPLAIN | **Not done.** Needs Ananya's restricted role and `psycopg` |
| Live `kubectl` readiness | **Not done.** Written, never executed against a cluster |
| Real fault/recovery run with Tooshita | **Not done.** |

Test results: 109 tests, all passing, run with
`python3 -m unittest discover -s tests/telemetry -t .`
No test here is a live measurement. Every fixture is `Source.FIXTURE`.

## Not yet proven

- **Workload markers are uncalibrated.** `WorkloadCalibration.verified` is `False` and the strict workload refuses to build until someone runs the probe against the pinned app and confirms each success marker. Nothing reported from a probe run is valid before that.
- **Capacity of the laptop is unmeasured.** Default is 10 rps, concurrency 8. If offered and achieved rate diverge the phase summary says so.
- **Cross-process clock skew is unmeasured.** Summaries state "clock skew versus runtime process unmeasured". Needs a handshake with Tooshita's runtime.
- **Provisional models.** `models.py` mirrors the shared interfaces. When Sachin's `backend/app/contracts/` lands, `_contracts.py` switches to it automatically; run the tests the same day to catch shape drift.
- **Dependencies not installed here:** `httpx` (live probe), `psycopg` (live EXPLAIN). Both are lazy imports. Sachin owns the lock file.

## Definitions teammates rely on

**Load profile** `boutique-mixed-v1`: browse 40%, product_view 25%, add_to_cart 20%, cart_read 10%, checkout 5%; 8 sessions, 10 rps requested, 5 s timeout, 20 s warm-up. Critical operations: add_to_cart, cart_read, checkout. Disable or document the upstream `loadgenerator` so two workloads never overlap.

**Freshness.** A `SystemStateSnapshot` is valid for 5 s (`valid_until`). After that it is *unavailable*, not healthy. Use `state_is_usable_for_safety(snapshot, at=now)`. Absent metrics are `null` and named in `missing_features`; CPU and memory are null on kind (no metrics-server).

**Percentiles.** Mergeable log-bucket histogram, 2% relative bucket width, 1 ms to 120 s, bucket geometric midpoint. Success-only latency population; timed-out attempts add no latency sample and are disclosed. Published only with at least 100 successful samples (team convention). p95 values are never averaged across windows.

**Window edges.** A phase closed at a runtime marker is rebuilt from the raw ring so samples after the marker are excluded. If the 120 s ring has already evicted the window start, the summary says the end edge may include late samples.

**Recovery.** `verified` needs all workloads ready, at least 3 critical successes per 10 s window, success rate within 0.02 of the frozen baseline, p95 within 1.5x baseline, for 3 consecutive windows; dated from the first healthy window. `failed` means observed for the full 180 s without recovering. `unknown` means observation was lost. Only `failed` scores zero.

**Score.** `0.40*A + 0.20*L + 0.25*R + 0.15*S`, version `score-1.0.0`. No total when any component is unknown, a load profile differs between phases, or the baseline was unhealthy. Weights are never renormalised. This is a team metric, not a Bosch score.

**Evidence.** `RunSummary.evidence_hash` is SHA-256 over every other field. `EvidenceRef` points into a real field and refuses a pointer that does not resolve.

## What is retained

| Data | Where | Lifetime |
|---|---|---|
| Raw per-request samples | RAM ring, max 9000 / 120 s | Until evicted or process exit |
| Learning observations | RAM queue, max 2000; pending 1000 / 300 s TTL | Until drained or evicted |
| `RunSummary` aggregates | Caller persists | Per report |
| Cookies, bodies, SQL, parameters, result rows, raw plans | **Never retained** | — |

## LOCAL DEMO prevention evidence

`python3 scripts/telemetry/run_prevention_acceptance.py` writes `reports/local_demo_prevention.json`.

It observes a real loopback target before and after an unsafe 90 s request against a 60 s cap, reads the target's own mutation counter, and checks the runtime's execution spy.

**Caveat:** the BLOCK decision is currently produced by a stand-in guard, because Tooshita's preflight guard is not merged. The evidence records this. The run verifies the *harness*, not the production guard. Replace `stub_guard` with her implementation when it lands.

A prevented attempt carries `resilience_score: null` and a reason; it is prevention evidence, not one of the three executed chaos mechanisms.

## Integration needs

- **Ananya:** live URL and protocol for the frontend, pinned SHA, product IDs, form field names; restricted DB role and DSN.
- **Tooshita:** fault injection/removal UTC markers, cleanup status, her preflight guard and execution spy.
- **Sachin:** contracts swap-in, `httpx`/`psycopg` in the lock, `GET /state` wiring.
- **Gautam:** consumes `LearningChannel.drain()`; features come from decision time only.

## Next steps, in order

1. Run the probe against Ananya's app and calibrate markers.
2. Measure laptop capacity; set the rate.
3. Capture a real baseline; freeze it.
4. Day 1 fault/recovery run with Tooshita; first real `RunSummary`.
5. Swap in the real guard for the prevention acceptance.
6. Day 2: three mechanisms, estimator against the real database.
