<!-- immediate-demo-update -->

## Latest user priority — local prevention demo within two hours

This addendum reflects the user's updated request and takes priority over the older milestone order below. Deliver one working injection attempt and one system response: stop the unsafe request BEFORE the fault executes and display an error popup. Do not interpret a blocked attempt as a successfully injected outage or a measured service crash.

The popup heading is **Error has occurred**. Its body must say **Injection blocked before execution**, identify the fixed rejection rule and show whether the target stayed healthy. The error occurred in the rejected request; do not claim that the target failed. Never execute a prohibited candidate merely to demonstrate danger.

Use the local demo path immediately when Kubernetes/team modules are unavailable. Label it LOCAL DEMO, use a real separate loopback HTTP target, submit a 90-second service-pause attempt against a fixed 60-second maximum, reject it before dispatch, latch STOP and verify zero dispatched mutations plus target health before/after. All accepted-duration requests remain blocked while execution is unavailable. Reset requires a healthy target; stale or unavailable target health must fail closed. This is prevention evidence, not one of the event's three executed chaos mechanisms.

Keep the existing shared ports and ownership boundaries for later Kubernetes integration. No one duplicates the team's SQL parser, live metric scoring or production runtime in the UI/API. The minimal local demonstration adapter is isolated and has no target mutation capability. Full Day 1/2 experiments, independent live watchdog, SQL execution and learning remain separate later gates.

### Builder and independent tester

- **Tooshita:** owns injection request handling, deterministic preflight rejection, STOP latch and safe reset; tests the runtime behavior.
- **Navya:** independently tests rejection BEFORE dispatch, zero target mutations, healthy target before/after, unavailable target rejection and numerical evidence; owns acceptance evidence.
- **Sachin:** composes ports/API, wires the injection button and accessible error popup, runs end-to-end UI checks and packages demo instructions.
- **Ananya:** supplies the real local target now and the enrolled Kubernetes target/proxy route later; checks reproducible launch.
- **Gautam:** supplies explanations from fixed rule/evidence facts; cannot authorize or bypass rejection. No LLM or learned model is needed for this demo.

### Immediate acceptance gate

1. Launch target, authenticated loopback API and dashboard from written steps.
2. Show a healthy target and a visible LOCAL DEMO provenance banner.
3. Click Inject test error: submit the unsafe proposal, return BLOCK and set STOP without dispatching a target mutation.
4. Show the error popup with rejection reason and healthy/unavailable target status, never a false recovery-success claim.
5. Verify target remains healthy; record request ID, rule, configured/requested duration, source, decision and executed=false. Retain only sanitized numeric/categorical evidence.
6. Test repeat attempts while STOP is latched, healthy reset, unavailable target, malformed requests, authentication and the popup. Mock checks are labelled separately from the real local HTTP test.
7. Keep GitHub upload separate from proof of local behavior. Publish only through available authenticated access; report an unavailable sign-in honestly.

---

# Bosch Resilience AI shared interfaces

These version 1 interfaces are the implementation contract between the five workstreams. Sachin turns this specification into Pydantic models, JSON schemas, OpenAPI and generated TypeScript types in the first integration commit. Each owner provides valid and invalid fixtures for the objects they produce. Fixtures are explicitly synthetic; the proposed fields and examples are not existing APIs or measured results.

## Repository boundaries

```text
bosch-resilience-ai/
  CONTEXT.md
  README.md
  prompts/
  contracts/v1/                       # Sachin: schemas and OpenAPI export
  fixtures/v1/                        # Each owner contributes synthetic fixtures
  application/online-boutique/         # Ananya: source pinned by gitlink or subtree
  infrastructure/kind/                # Ananya
  infrastructure/k8s/{base,overlays}/  # Ananya; runtime RBAC reviewed by Tooshita
  backend/app/
    main.py                           # Sachin
    contracts/                        # Sachin: authoritative Pydantic definitions
    api/                              # Sachin: routes and composition
    platform/                         # Ananya: inventory and discovery
    runtime/                          # Tooshita: mutations, SQL execution and watchdog
    telemetry/                        # Navya: probes, EXPLAIN adapter, summaries and scoring
    intelligence/                     # Gautam: AST, risk, reasoning and learner
  frontend/                           # Sachin
  scripts/platform/                   # Ananya: setup, smoke and teardown
  scripts/runtime/                    # Tooshita: start watchdog, STOP and reconcile
  tests/{platform,runtime,telemetry,intelligence,contracts,integration}/
  docs/{platform,runtime,telemetry,intelligence,integration}/
  reports/                            # Sanitized final evidence and findings only
```

Use one Python import layout and root dependency lock coordinated by Sachin. Module owners do not create independent backend applications, duplicate shared models, or change other owners' paths silently. Generated files identify their source and are regenerated through one documented command.

## Common serialization rules

Every exported object includes `schema_version: "1.0"`. Observed objects include `source: live | fixture | replay`, UTC ISO timestamps and a random ID. Display local times separately. Numeric units use explicit names: `_ms`, `_seconds`, `_bytes`; fractions use 0 to 1. Unknown values are null with a reason, never zero. Reject NaN, infinity and unexpected fields in action contracts. Enumerations are closed.

Use monotonic time for durations and deadlines inside one process; UTC timestamps align exported events. Track clock uncertainty across processes. HTTP errors have `{code, message, retryable, correlation_id, details}` with sanitized details. Never include SQL, parameters, credentials or database result rows in error bodies or logs.

Random IDs provide correlation. Raw SQL and parameter hashes used to bind approval live only in the in-memory intent vault; do not publish them as a durable query history. An exported `evidence_ref` contains a `run_id`, an evidence summary hash and a JSON pointer to a real field in that summary.

## Shared object definitions

| Object | Producer | Required fields and meaning |
|---|---|---|
| SandboxIdentity | Ananya enrolls; Tooshita verifies | `sandbox_id`, `context`, `api_endpoint`, `ca_fingerprint`, `namespace_uids`, `enrolled_at`. Local operational configuration, no credential material. |
| TopologySnapshot | Ananya | `graph_id`, `upstream_sha`, `generated_at`, `sandbox_id`, `namespace`, `nodes[]`, `edges[]`, `manual_corrections[]`. |
| SystemStateSnapshot | Navya | `state_id`, `captured_at`, `valid_until`, `sandbox_id`, `ready_by_workload`, `request_rate`, `error_rate`, `p95_ms`, `db_connections_used`, `db_connections_limit`, optional resource utilization, `missing_features[]`, `active_fault_id`. |
| PlanEstimate | Navya | `request_id`, `captured_at`, `plan_source`, `estimated_rows`, `estimated_width_bytes`, `estimated_output_bytes`, `planner_cost`, `node_categories[]`, `stats_quality`, `uncertainty[]`. Sanitized numeric plan summary; no SQL or raw plan expressions. |
| QueryDecision | Gautam | `decision_id`, `request_id`, `decision: ALLOW|WARN|BLOCK`, `rule_ids[]`, `policy_version`, `state_id`, `plan_estimate`, `prediction`, `reasons[]`, `recommendations[]`, `executed: false`, optional expiring `intent_id`. |
| ExperimentProposal | Gautam or operator | `proposal_id`, `catalog_id`, `sandbox_id`, `target_id`, `fault_parameters`, `hypothesis`, `reason`, `expected_blast_radius[]`, `load_profile_id`, `abort_limits`, `policy_version`, `expires_at`. No arbitrary selectors, manifests or commands. |
| ApprovalRecord | Tooshita via authenticated API | `approval_id`, `proposal_hash`, `actor_ref`, `approved_at`, `expires_at`, `scope`, `single_use`. Bind exact target/parameters/policy, validate ownership and consume atomically. |
| RunEvent | Tooshita or Navya | `run_id`, `sequence`, `timestamp`, `event_type`, `state`, sanitized `details`, `source`. Bounded live stream; important transitions compacted into summary. |
| RunSummary | Navya with runtime facts | `run_id`, `catalog_id`, `sandbox_id`, `topology_hash`, `load_profile`, `phase_summaries`, `timeline_markers[]`, `stop_reason`, `cleanup_status`, `recovery_status`, `recovery_seconds`, `data_quality`, `evidence_hash`. |
| AnalysisResult | Gautam | `run_id`, `mode: live_model|deterministic_fallback|fixture`, `measured_facts[]`, `hypotheses[]`, `alternatives[]`, `recommendations[]`, `evidence_refs[]`, `limitations[]`. Numeric evidence copied or referenced, not invented. |
| LearningObservation | Navya/runtime to Gautam | `request_id`, `captured_at`, `state_id`, `kind: arrival|completed|censored|failed`, fixed `features` on arrival, nullable outcome metrics, `source`, `active_fault_id`. Outcome-only events may omit features; join them to the bounded pending entry. RAM only. |
| LearningStatus | Gautam | `learner_version`, `started_at`, `updates`, `pending_count`, `dropped_events`, `drift_status`, `prediction_error`, `retained_raw_examples: 0`, configured queue/model limits, `persistence: disabled`. No sample archive. |
| ActionProposal | Gautam, Day 2 | `action_id`, `template_id`, `sandbox_id`, `target_id`, `parameters`, `preconditions`, `rationale`, `evidence_refs`, `observation_seconds`, `auto_revert_requested`. Human and policy approve it. |

Topology nodes include resource name/kind/UID, actual selector, ports, desired replicas, statefulness and confidence. Edges use `caller → dependency` and include protocol, provenance and confidence. Use reverse traversal to identify callers affected by dependency failure. Selectors are derived from the validated registry, not from model text.

Each phase summary contains its actual time window and per-operation `{attempted, succeeded, failed, error_categories, success_rate, p50_ms, p95_ms, percentile_method, sample_count, missing[]}`. Record endpoint identity and workload settings; use numbers for ready/desired replicas rather than a string such as `11/11`. Store the minimal ready-state extrema and relevant transition markers.

Supporting shared types must also be defined in the first contract commit:

| Type | Fields |
|---|---|
| VerifiedTarget | `target_id`, `sandbox_id`, `namespace`, `kind`, `name`, `uid`, actual `selector`, allowed fault/template IDs, verified dependency/route metadata and provenance. |
| LoadProfile | `load_profile_id`, operation mix, concurrency, requested rate, timeouts, warm-up and measurement window settings, generator version. |
| PhaseSummary | `phase`, start/end timestamps, operation aggregates, actual offered/achieved rate, ready/desired extrema, quality notes and percentile method. |
| ValidationResult | `proposal_id`, `proposal_hash`, `valid`, `checks[]`, rejection codes, checked sandbox/target identities, policy version and expiry. |
| RunHandle | `run_id`, `proposal_id`, `state`, accepted timestamp and status/events URLs. |
| StopReceipt | `request_id`, `accepted_at`, STOP latch state, affected run IDs and current cleanup state; never an unconditional recovery-success flag. |
| IntentHandle | Random `intent_id`, decision/actor/target references and expiry; no SQL or parameters. |
| BoundedReadHandle | Random `job_id`, current state, declared row/byte/time budgets and eventual completion/cancellation/truncation status with numeric outcome counts. |
| Prediction | Nullable `duration_ms`, learner version, pre-update sample count, `cold_start`, `drift_status`, quality note; an estimate, not a probability of failure. |

Keep these types available to fake adapters as well as real implementations. No owner invents a parallel incompatible return shape while awaiting integration.

`cleanup_status` is `verified | failed | not_applicable | pending`; `recovery_status` is `verified | failed | unknown | pending`. For deleted Pods, cleanup can be `not_applicable` while replacement and functional recovery still require verification. Do not conflate restored configuration with recovered application behavior.

## Internal module ports

These ports are Python interfaces, not separate public services. Async signatures may use the project's chosen typing convention. Raw inputs labelled transient must never be automatically serialized or logged.

```text
PlatformPort.get_topology(sandbox_id) -> TopologySnapshot
PlatformPort.get_target(target_id) -> VerifiedTarget

TelemetryPort.get_state(sandbox_id) -> SystemStateSnapshot
TelemetryPort.begin_phase(run_id, phase, load_profile) -> phase_handle
TelemetryPort.close_phase(phase_handle) -> PhaseSummary
TelemetryPort.finalize_run(run_id, runtime_facts) -> RunSummary
PlanEstimator.estimate(validated_sql, parameters, target_id) -> PlanEstimate
  # SQL and parameters are transient trusted internal inputs after AST validation.

IntelligencePort.analyze_query(transient_request, state, estimator) -> QueryDecision
IntelligencePort.plan(topology, state, catalog, coverage) -> ExperimentProposal[]
IntelligencePort.analyze_run(summary, topology) -> AnalysisResult
IntelligencePort.observe(observation) -> LearningStatus
IntelligencePort.get_learning_status() -> LearningStatus

RuntimePort.validate(proposal) -> ValidationResult
RuntimePort.approve(proposal_id, actor, proposal_hash) -> ApprovalRecord
RuntimePort.start(proposal_id, approval_id, idempotency_key) -> RunHandle
RuntimePort.stop(run_id, actor) -> StopReceipt
RuntimePort.emergency_stop(sandbox_id, actor) -> StopReceipt
RuntimePort.reconcile(sandbox_id) -> CleanupSummary
RuntimePort.stage_query(transient_validated_sql, decision, actor) -> IntentHandle
RuntimePort.approve_query(intent_id, actor) -> SingleUseToken
RuntimePort.execute_query(token, actor, idempotency_key) -> BoundedReadHandle
  # The worker revalidates current state/policy and SQL identity before execution.
```

The API coordinates calls; the runtime owns mutation authorization, locks, tokens and jobs. Query analysis must stage an intent only for a potentially executable decision. BLOCK destroys transient SQL once analysis completes. WARN requires the prescribed review and revalidation and cannot override a hard block. Expired intents require resubmission.

The runtime can invoke the intelligence parser/policy and Navya's estimator locally for pre-execution revalidation. It never calls the LLM for permission. If those deterministic checks are unavailable, reject execution.

## Proposed public API

All public routes use `/api/v1`. Sachin owns registration and authentication; module owners implement the port behavior.

| Method and path | Behavior owner | Behavior |
|---|---|---|
| GET `/health` | Sachin | Backend and dependency readiness, separate from target health |
| GET `/topology` | Ananya | Graph and verified target inventory |
| GET `/state` | Navya | Current system snapshot and freshness |
| GET `/experiments/catalog` | Tooshita | Supported bounded faults |
| POST `/experiments/plan` | Gautam | Ranked proposals; no execution |
| POST `/experiments/validate` | Tooshita | Dry validation; no mutation |
| POST `/experiments/{proposal_id}/approve` | Tooshita | Exact proposal approval |
| POST `/experiments/{proposal_id}/start` | Tooshita | Return 202 and run ID for approved job |
| GET `/runs/{run_id}` | Tooshita and Navya | State, summarized metrics, recovery and quality |
| GET `/runs/{run_id}/events` | Sachin | SSE using bounded event stream; polling fallback |
| POST `/runs/{run_id}/stop` | Tooshita | Stop fault, initiate cleanup and return receipt |
| POST `/emergency-stop` | Tooshita | Latch STOP, block mutations and request cleanup |
| POST `/safety/reset` | Tooshita | Operator reset after verified reconciliation |
| POST `/sql/analyze` | Gautam with Navya | AST, safe plan and deterministic decision; no candidate execution |
| POST `/sql/intents/{intent_id}/approve` | Tooshita | Actor-bound single-use token |
| POST `/sql/execute-approved` | Tooshita | Bounded approved read; no replacement SQL accepted |
| POST `/sql/jobs/{job_id}/cancel` | Tooshita | Cancel owned active read; close cursor/connection |
| GET `/learning/status` | Gautam | Counters, drift and model memory policy |
| GET `/runs/{run_id}/analysis` | Gautam | Evidence grounded analysis |
| GET `/reports/{run_id}` | Sachin | Sanitized Markdown or HTML export |

The paths above are appended to `/api/v1`, so `GET /health` means `GET /api/v1/health`. Day 2 action endpoints are defined in the rollback specification and are disabled on Day 1.

Start returns acceptance, never a false completed result. Use authenticated localhost operators, exact CORS origins, request size limits, bounded timeouts and idempotency keys. SSE disconnection does not stop the watchdog. A consumed SQL token is never executed again after HTTP retry; return prior job status or require a fresh analysis.

## Experiment state machine

```text
DRAFT -> VALIDATED -> AWAITING_APPROVAL -> BASELINING -> PREFLIGHT
      -> INJECTING -> OBSERVING -> CLEANING_UP -> VERIFYING_RECOVERY
      -> ANALYZING -> COMPLETED
```

Validation failures end in `REJECTED`. Active faults on STOP, TTL, lost metrics or internal error transition through cleanup before a terminal outcome is chosen. Use `ABORTED`, `CANCELLED`, `FAILED` and `CLEANUP_FAILED` as terminal outcomes with explicit reasons. `COMPLETED` means the workflow completed; the report separately states whether the experiment hypothesis was supported and recovery verified. A cleanup failure latches STOP. Analysis failure can still produce a deterministic report of measured facts.

Run IDs are allocated by the runtime when the validated proposal is created or staged. Approval is bound to that proposal version. Ownership and expiry are rechecked after the baseline and immediately before injection to prevent time-of-check/time-of-use errors.

## Scoring contract

Calculate numbers in Navya's deterministic scoring module. Publish the formula and configuration before use. The proposed team resilience score is `0.40*A + 0.20*L + 0.25*R + 0.15*S`, each component from 0 to 100. It is not an official Bosch metric.

One reproducible initial normalization is:

```text
A = 100 * fault_successes / fault_attempts
L = 100 * clip((latency_abort_ms - fault_p95_ms)
               / (latency_abort_ms - baseline_p95_ms), 0, 1)
R = 100 * clip(1 - recovery_seconds / recovery_timeout_seconds, 0, 1)
S = 100 if workflow constraints and required cleanup/recovery checks passed, else 0
```

Require positive attempts, sufficient samples, healthy baseline and `latency_abort_ms > baseline_p95_ms`. Return no total when required components are unknown; do not silently renormalize. If recovery was observed to fail by the full timeout, R is 0; if recovery observation was lost, R is unknown. Show components and guardrails even when a total cannot be computed. Use equal experiment weights only when all included results are valid; otherwise show incomplete coverage without an aggregate score.

Gautam's query risk remains a separate deterministic heuristic, labelled risk rather than crash probability. Rank recommendations using a documented rubric; model explanations may not supply invented numeric benefits. Status is `predicted`, `supported_in_this_run`, `not_supported` or `insufficient_evidence`. Single-run improvement does not establish a general guarantee.

## Independent implementation and integration tests

Each module exports a real and fake adapter using the same types. Fixtures cover healthy, missing/stale telemetry, rejected proposal, failed cleanup, blocked query, model outage and insufficient evidence. Fake tests must never be presented as live runs.

CI checks schema consistency and rejects incompatible edits, then exercises contracts through fake ports. Local integration tests cover actual injection effects, cleanup, functional recovery and memory retention. Capture tests that were actually run and their results. The implementation task includes turning this proposed contract into tested code; this pack does not claim that those checks have passed.
