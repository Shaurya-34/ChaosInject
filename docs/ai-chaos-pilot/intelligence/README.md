# Intelligence workstream (Gautam)

> **Removed (gauthamop/drop-sql-guard):** the SQL AST guard (`sql_ast.py`), query advice, query policy, the PostgreSQL plan estimator and the `/api/v1/sql/*` routes were dropped as off-topic for ChaosInject. Sections below that describe them are historical. The streaming learner, its features, the `/api/v1/learning/status` route and the telemetry learning channel were removed later (gauthamop/remove-learner); `river` is no longer a dependency. The current agents are `injection_agent.py` and `monitoring_agent.py` (see the chaos dashboard).

This workstream covers the SQL guard and query policy, the streaming learner, the experiment planner, evidence-grounded diagnosis and advice, the reasoning adapters, and a disabled interface for the Day 2 rollback proposals.

| Location | Contents |
|---|---|
| `backend/app/intelligence/` | Code. `IntelligenceService` implements `app.api.ports.IntelligencePort` (async) using the shared models in `app.contracts`. |
| `tests/intelligence/` | Tests |
| `fixtures/v1/intelligence/` | Synthetic fixtures |

Everything was exercised with **fake adapters and synthetic data only**: no cluster, PostgreSQL lab or live model. Nothing in this document is a live measurement.

## Status

| Area | State |
|---|---|
| AST guard (`sql_ast.py`) | Implemented and tested. A default-deny walker over pglast 8.5. |
| Query policy (`policy.py`) | Implemented and tested. Version `sqlguard-2026.10.1`. |
| Safer-query advice (`query_advice.py`) | Implemented. Returns structured guidance rendered as text; never rewritten SQL. |
| Adaptive statistics (Day 1) | Implemented: EWMA moments, counters, experiment coverage, moving-average baseline. |
| Incremental predictor (Day 2) | Built early and **advisory only**: River StandardScaler → LinearRegression on log1p(ms). Policy uses it only to tighten a decision, and only when it is warm and not drifting. |
| Planner (`planner.py`) | Implemented. Runs on Sachin's `Catalog` and `TopologySnapshot`. |
| Diagnosis and advisor (`diagnosis.py`, `dependency_map.py`) | Implemented. Uses a hard/soft dependency map read from the vendored upstream source. |
| Reasoning adapters (`reasoning.py`) | Deterministic (default), mock and live-transport adapters exist. **No live provider is wired**: no provider, model, quota or spend is assumed. |
| Rollback proposals (`rollback.py`) | Interface only. Raises `RollbackDisabled` while `ADAPTIVE_ROLLBACK_ENABLED` is false. |
| API wiring | Not wired in. Fixture/demo modes still use `FixtureIntelligence`. A live `LIVE_PORTS_FACTORY` can construct `IntelligenceService(allowlist)`. `tests/intelligence/test_integration_api.py` plugs it into the real FastAPI app. |

## Port surface

```text
await analyze_query(transient_request, state, estimator) -> QueryDecision
await revalidate(transient_request, state, estimator, request_id) -> QueryDecision   # runtime pre-execution check; no learning side effects
await plan(topology, state, catalog, coverage) -> [ExperimentProposal]               # coverage {} -> uses the learner's coverage
await analyze_run(summary, topology) -> AnalysisResult
await observe(LearningObservation) -> LearningStatus
await get_learning_status() -> LearningStatus
note_run_target(run_id, target_id)                                                    # see "RunSummary target" below
```

**Inputs**
- `transient_request` is the API's `QueryRequest` (`sql`, `parameters`, `target_id`). The module reads it and never stores it.
- The estimator may be async or sync, and may return the shared `PlanEstimate` or anything that validates into one. Failure, or `plan_source="unavailable"`, results in BLOCK with rule `EST-001`.

**Outputs**
- `QueryDecision.executed` is always `False`, and `intent_id` is left for the API to fill.
- Recommendations are plain strings that begin with their template ID.
- The heuristic risk score appears as one line in `reasons`, labelled as not a probability.

## SQL parameters

`QueryRequest.parameters` is a mapping. Keys must be positional: `"1".."n"`, or `"$1".."$n"`, contiguous from 1. They are converted to a positional list for `$n` placeholders. Any other keys are rejected with rule `SQL-015`. The guard never substitutes values into SQL text.

## Supported SQL grammar (`select-subset-1`)

One plain `SELECT`, with:

- **FROM**: exactly one item, either an allowlisted table or a chain of `INNER`/`LEFT JOIN`s. Each join needs `USING`, or an `ON` containing a `col = col` equality, so cross joins and `ON true` are rejected.
- **Select list**:
  - allowlisted columns, `*` and `alias.*`;
  - constants and `COALESCE`;
  - casts to `int2`, `int4`, `int8`, `numeric`, `float8`, `text`, `varchar`, `date`, `timestamp`, `timestamptz` and `bool`;
  - scalar functions `lower`, `upper`, `length`, `abs`, `round`, `floor`, `ceil`, `date_trunc`, `greatest` and `least`;
  - aggregates `count`, `sum`, `avg`, `min` and `max`, including `count(*)`.
- **WHERE and HAVING**: comparisons, arithmetic, `IN (list)`, `BETWEEN`, `LIKE`/`ILIKE`, `IS [NOT] NULL`, and `AND`/`OR`/`NOT`.
- **Other clauses**: `GROUP BY`; `ORDER BY` on a column, output alias or ordinal; plain `DISTINCT`; and `LIMIT`/`OFFSET` as integer constants or `$n`.

Everything else is rejected. That includes CTEs (and so writable CTEs), sub-queries, set operations, window functions, aggregate `FILTER`/`ORDER`, `SELECT INTO`, row locks, `DISTINCT ON`, `WITH TIES`, table functions, `TABLESAMPLE`, `ANY`/`ALL`, and any non-SELECT or second statement.

Limits: 8 KiB of SQL and 32 parameters. Rejection reasons are fixed strings, because pglast parse errors can echo the input.

## Policy rules (`sqlguard-2026.10.1`)

The decision is the most severe rule triggered. State and predictions can only add rules, never remove one. `QueryPolicyConfig` is frozen, and the learner holds no reference to it.

| Rule | Decision | Trigger (demo thresholds, tunable) |
|---|---|---|
| SQL-001…015 | BLOCK | Grammar or allowlist failures (see `policy.RULES`) |
| EST-001 | BLOCK | No usable plan estimate: no estimator, estimator error, `plan_source="unavailable"`, or unknown rows/bytes |
| EST-002 | WARN | `stats_quality` is `stale` or `unknown` |
| OUT-001 | BLOCK | Estimated output > 32 MiB |
| OUT-002 | BLOCK | Estimated rows > 50,000 |
| OUT-003 | BLOCK | Row-producing query, no LIMIT, estimated rows > 1,000 (the interactive budget) |
| OUT-004 | WARN | Estimate > 1,000 rows or > 1 MiB; the output will be truncated |
| COST-001 | WARN | Planner cost > 100,000 (planner units, **not ms**) |
| SORT-001 | WARN | LIMIT + ORDER BY with an explicit sort on a non-indexed key |
| STATE-001 | WARN (can be configured to BLOCK) | State missing, or past `valid_until` |
| STATE-002 / STATE-003 | WARN / BLOCK | DB connection utilisation ≥ 0.80 / ≥ 0.95 |
| STATE-004 | BLOCK | `active_fault_id` is set: SQL pauses during chaos runs so effects can be attributed |
| PRED-001 | WARN | A warm, stable learned duration estimate > 5,000 ms (advisory) |

Results with the fixture estimator over the placeholder 10,000-row `lab.products`:

| Query | Decision |
|---|---|
| `WHERE id = $1` | ALLOW |
| `SELECT *` | BLOCK, `OUT-003` |
| `count(*)` | ALLOW |
| `ORDER BY name LIMIT 10` | WARN, `SORT-001` |

## Learning

**Internal feature vector (`features-1`, 34 fixed dimensions)**
- Plan estimate: log of estimated rows, bytes and cost, each with a missing flag.
- Query structure: clamped counts and boolean flags.
- Plan node categories.
- State at decision time: p95, error rate, connection utilisation and ready fraction, each with a missing flag; plus whether a fault is active and whether the state is stale.

It never contains SQL text, literals, parameters, LIMIT values, identities, table names or fingerprints. Arrivals that telemetry reports with the shared 8-field `LearningFeatures` are mapped onto the same vector; the structure-only dimensions are set to 0 and the plan dimensions are marked missing.

**Processing order**
1. Predict at arrival.
2. Park only the numeric tuple, keyed by a random request ID.
3. When the outcome arrives, measure the error against the arrival-time prediction, then call `learn_one` exactly once.
4. Drop the parked entry.

Blocked requests are never parked. Censored and failed outcomes are counted but not trained. Duplicate and late outcomes are counted, not trained. Fixture traffic is rejected by a live learner (`source` must match). State features do not update the baseline while a fault is active.

**Limits** (also reported in `LearningStatus`)

| Item | Limit |
|---|---|
| Queue | 256 |
| Pending entries | 128 |
| Pending TTL | 120 s |
| Recent consumed IDs | 256 |
| Coverage entries | 64 |
| `model_limit_bytes` | 65,536 |

Nothing is persisted, and `retained_raw_examples` is always 0. `learner.diagnostics()` exposes the counters and baseline error that are not part of the shared `LearningStatus`.

**Cold start and drift**
- Cold start lasts until 20 completed observations (a demo convention).
- Drift is flagged when, after at least 30 errors, the short EWMA of |residual| (α 0.3) exceeds 2.5 × max(long EWMA (α 0.03), 0.1) for 8 consecutive updates. The model then resets.
- `drift_status` takes the values `cold_start`, `stable`, `drift` and `unknown`.

**Observed synthetic behaviour (not a live claim)**
- On a 300-sample synthetic stream, the model's pre-update MAE on log1p(ms) was about 0.04, against about 1.5 for the moving-average baseline.
- After a 200× slowdown, drift was flagged at the 8th shifted sample.

## Planner rubric (`planner-rubric-1`)

```text
score = 0.35 impact + 0.25 criticality + 0.15 uncertainty + 0.15 coverage_gap
      + 0.05 reversibility − 0.05 execution_cost
```

- **Impact** counts the transitive callers of the target (a reverse walk over caller → dependency edges).
- **Criticality** comes from a reviewed judgement table (`DEFAULT_CRITICALITY`).

**Shape rules per catalog ID** (`DEFAULT_RULES`)
- **EXP-00 and EXP-01**: stateless Deployments only.
- **EXP-02 and EXP-03**: only targets behind a verified proxy route (`route_targets`, by default `redis-cart`).

**Excluded before scoring**
- State is stale or missing, or belongs to a different sandbox.
- A fault is already active.
- The baseline error rate is unknown or above 2%.
- The catalog entry is unavailable.
- The target's readiness is unknown, or it is not ready.

**Proposals**
- Carry bounded `FaultParameters`: duration ≤ min(30, the catalog maximum).
- Carry the configured `AbortLimits`.
- Include a predicted hypothesis that names the hard and soft operations from the dependency map.
- Expire after 15 minutes.

Tooshita's runtime validation stays authoritative.

## Diagnosis and advice

**Dependency map** (`dependency_map.py`)

It was read from the vendored upstream source (`frontend/main.go`, `handlers.go`, `rpc.go` and `checkoutservice/main.go`):
- Cart, product catalog, currency, shipping and payment failures are fatal (HTTP 500 or a gRPC error).
- Ad, recommendation and confirmation-email failures are only logged.

Operation names match Navya's workload: `browse`, `product_view`, `add_to_cart`, `cart_read` and `checkout`, plus the fixture's `cart`.

**Hypotheses** (at least 20 samples per phase)

| Dependency type | Outcome | Status |
|---|---|---|
| Hard | The operation degraded (≥ 5 percentage points or p95 ×1.5) | `supported_in_this_run` |
| Hard | Unchanged | `not_supported` |
| Soft | Unchanged (resilience confirmed) | `supported_in_this_run` |
| Soft | Degraded (the fallback failed) | `not_supported` |
| Unmapped | Degraded | Flagged as a confounder |

The diagnosis also checks functional recovery against the baseline.

**Priority** (1 = highest)

| Priority | Finding |
|---|---|
| 1 | Supported degradation of a business-critical operation, or failed recovery |
| 2 | Supported degradation of another operation |
| 3 | Insufficient or unexpected evidence |
| 4 | Expectation not supported |
| 5 | Resilience confirmed |

**Remedies**
- Ranked by `0.5 severity + 0.3 criticality + 0.2 applicability`.
- Finding priority = `1 + round(4 × (1 − score))`.
- Each one carries trade-offs and a verification run, and is always `predicted`.
- The Redis remedy states that a plain replica is not high availability.
- The replica remedy states that a PodDisruptionBudget does not stop direct Pod deletion.

**RunSummary target**

`RunSummary` does not record the injected target. Without it, findings are reported per operation, with a stated limitation and no dependency attribution. The live factory can call `note_run_target(run_id, target_id)` (bounded to 256 runs), or `target_id` can be added to `RunSummary`; that is a proposed contract change for Sachin.

The analysis output passes `app.api.evidence.checked_evidence`: matching source, matching hash, and pointers that resolve.

## Reasoning modes

| `INTELLIGENCE_REASONING_MODE` | Behaviour |
|---|---|
| `deterministic` (default) | No model call. `mode = deterministic_fallback`. |
| `mock` | Canned responses. `mode = fixture`. For tests only. |
| `live` | Needs `INTELLIGENCE_LIVE_MODEL` **and** a transport registered by the live factory. Otherwise it falls back to deterministic and records why. |

A model may only attach explanations of 400 characters or fewer to existing hypothesis IDs. Its output is rejected if it:
- does not match the schema (for example, it adds `approve` or `status`);
- refers to an unknown hypothesis;
- cites an evidence pointer that does not resolve;
- contains a number that is not among the measured facts.

Model calls are retried at most twice, time out after 20 s, and run off the event loop. The fixed system prompt tells the model to treat metadata as untrusted JSON data.

## Integration needs

**Sachin**
- Wire `IntelligenceService` into the live ports factory.
- Decide between adding `target_id` to `RunSummary` and having the factory call `note_run_target`.
- Register a live transport only after a provider, model and spend limit are approved.

**Navya**
- `PlanEstimator.estimate` receives a positional `list`; the port in `ports.py` says `dict`. Agree one shape.
- The estimate should validate as the shared `PlanEstimate`.
- psycopg uses `%s` placeholders, while the guard validates `$n`. The EXPLAIN adapter needs to bind `$n` server-side or translate them.
- Forward completed/censored/failed `LearningObservation`s with the decision's `request_id`.

**Ananya**
- Provide the real lab schema allowlist (tables, columns, indexed columns, primary keys) to replace `fakes.LAB_ALLOWLIST_DATA`.
- Confirm that `route_targets` matches the Toxiproxy overlay.

**Tooshita**
- Call `revalidate(...)` before execution, and reject execution if it fails.
- Forward outcomes with the same `request_id`.

## Dependencies

`pglast==8.5` and `river==0.26.1`. River pulls in numpy 2.5.3, scipy 1.18.1 and narwhals 2.27.1. All of them are in `pyproject.toml`, `requirements.lock` and the roots in `scripts/lock_dependencies.py`. Linux wheels for CPython 3.12 exist for every pinned version.

## Running

```bash
python -m pytest tests/intelligence -q
```

```bash
PYTHONPATH=backend python -m app.intelligence.export_fixtures
```

The second command regenerates the synthetic fixtures.

## Not claimed

- No secure erasure.
- No claim that model weights carry zero privacy risk.
- No calibrated crash probability.
- No general causal conclusions from a single run.
- No live-model or live-cluster behaviour.
