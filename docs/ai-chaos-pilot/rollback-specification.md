# Bosch Resilience AI Day 2 adaptive rollback specification

Implement this after the Day 1 chaos safety path and shared interfaces work. Day 1 permits an interface stub and written tests for expected behavior, with `ADAPTIVE_ROLLBACK_ENABLED=false`; it does not include an active adaptive rollback controller.

The intended behavior is specific: the intelligence layer observes requests and fresh system state, recognizes that a previously approved reversible configuration change may be causing regression, and proposes reverting that change. Deterministic runtime policy decides whether reversal is authorized and safe. Cleanup of chaos faults continues to use the existing watchdog and does not wait for this feature.

## First supported action

Use one template, `stateless_replicas`, on an allowlisted stateless Deployment. The operator reviews the exact old and new replica count and may explicitly grant permission for automatic reversion during a short observation window. Choose a real target and a change whose capacity effects can be measured safely; do not promise that every replica change creates a regression.

Do not extend the first version to Redis replication, database writes, PVCs, namespace deletion, secrets, client code rewrites or arbitrary generated patches. A PodDisruptionBudget does not prevent arbitrary direct Pod deletion and must not be presented as protection for that injector.

## Action state machine

```text
PROPOSED -> VALIDATED -> APPROVED -> APPLYING -> OBSERVING
    -> ACCEPTED
    -> REVERT_REQUESTED -> REVERTING -> VERIFYING_REVERT -> REVERTED
```

Exceptional outcomes are `REJECTED`, `EXPIRED`, `APPLY_FAILED`, `STATE_CONFLICT`, `REVERT_FAILED` and `INCONCLUSIVE`. A reverse operation returning success does not establish recovered behavior. Revert failure or unexplained state conflict latches STOP and blocks new mutations until the operator reconciles the target.

## Information retained for safe reversal

Before changing the target, atomically write the minimal operational journal: random action ID, sandbox identity, target resource UID, prior value of the modified field, expected applied value, approval scope/expiry, baseline summary hash, observation deadline and status. Use local file permissions and a bounded journal format. Do not store raw requests, SQL, parameters, result rows or a complete object containing secrets.

Restore only fields owned by this action. Check that the resource UID and modified-field value still match the expected applied state, using a conditional patch or equivalent conflict check. If somebody changed that field after application, refuse to overwrite it. Resource versions may change due to controller status updates; do not rely on full resource-version equality alone as the only conflict test.

Delete resolved journal entries after verified reversion or accepted completion and the configured grace period. Unresolved entries remain until recovery is resolved. The journal supports safety across process restarts; it is not model training history. A crash invalidates RAM learning state and query approvals, while journal reconciliation handles active configuration changes.

## Regression conditions

Freeze hard limits and the load profile before application. Use fresh, sufficient measurements of business transaction errors, p95 latency and readiness. The learned predictor may flag a candidate regression, but cannot authorize reversion alone.

Example configurable soft trigger: error rate increases by at least five percentage points or p95 exceeds 1.5 times the healthy pre-change baseline for three consecutive ten second windows with adequate samples. An already approved hard safety limit may trigger immediate action. These values are demo settings to validate, not universal operating thresholds.

Required conditions for autonomous reversion:

1. Exact action has explicit auto-revert opt-in and that permission remains within its observation window.
2. Sandbox identity, target UID and modified-field value still match.
3. Required telemetry is fresh and meets the sample requirement.
4. Deterministic trigger conditions are satisfied; explanation references real fields.
5. No unrelated chaos experiment or mutation holds the lock.
6. Reversal budget is unused: at most one automatic revert per action, no repeated apply/revert loop.

A telemetry outage is a separate safety condition, not proof that the change caused a regression. Use the preapproved fail-safe policy or request operator attention. After reverting, wait for the prior replica state and sustained functional health with a timeout. Record the observed outcome and uncertainty, without asserting causation from a single correlation.

## API additions

Tooshita owns the runtime behavior, Gautam owns proposals and evidence rules, Navya owns observation summaries, and Sachin registers these routes:

```text
POST /api/v1/actions/propose
POST /api/v1/actions/{action_id}/approve
POST /api/v1/actions/{action_id}/apply
POST /api/v1/actions/{action_id}/revert
GET  /api/v1/actions/{action_id}
```

No route accepts arbitrary Kubernetes patches. Approval identifies the exact rendered diff and whether automatic revert is permitted. GET returns safe state and evidence references, not sensitive preimages. Manual revert remains available to the authorized operator even when the learning process is unavailable.

## Verification and demo

Test normal acceptance, manual reversion, deterministic regression, stale metrics, missing opt-in, expired approval, concurrent mutation, changed resource UID, external field change, double revert, watchdog restart and failed restoration. A mock regression validates logic but is labelled synthetic. Demonstrate one real local configuration change and reversion with measured evidence if the feature is included in the final demo.

Do not repeatedly worsen the application to manufacture an effect. If no measurable regression occurs, report acceptance or inconclusive evidence, and use a clearly labelled controller test to explain the rejection/rollback logic. Keep the three required chaos experiments and final deliverables ahead of broadening rollback.

For approved read-only SQL, use cancellation, deadline enforcement and stream closure; there is no committed write to reverse. Do not claim to recover time, CPU already spent, or output already delivered. PostgreSQL rollback concerns uncommitted transaction changes. [PostgreSQL documentation](https://www.postgresql.org/docs/current/sql-rollback.html).
