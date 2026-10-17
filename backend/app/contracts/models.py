"""Authoritative contracts. Generated JSON/OpenAPI/TypeScript derive from here."""
from datetime import datetime
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, model_validator

Source = Literal["live", "fixture", "replay"]
Fraction = Annotated[float, Field(ge=0, le=1)]
Positive = Annotated[float, Field(ge=0)]
Count = Annotated[int, Field(ge=0)]
Cleanup = Literal["verified", "failed", "not_applicable", "pending"]
Recovery = Literal["verified", "failed", "unknown", "pending"]
RunState = Literal["DRAFT", "VALIDATED", "AWAITING_APPROVAL", "BASELINING", "PREFLIGHT", "INJECTING", "OBSERVING", "CLEANING_UP", "VERIFYING_RECOVERY", "ANALYZING", "COMPLETED", "REJECTED", "ABORTED", "CANCELLED", "FAILED", "CLEANUP_FAILED"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_default=True)
    schema_version: Literal["1.0"] = "1.0"


class Observed(Contract):
    source: Source


class SandboxIdentity(Contract):
    sandbox_id: str
    context: str
    api_endpoint: str
    ca_fingerprint: str
    namespace_uids: dict[str, str]
    enrolled_at: AwareDatetime


class TopologyNode(Contract):
    target_id: str
    name: str
    kind: Literal["Deployment", "StatefulSet", "Service", "Pod"]
    uid: str
    selector: dict[str, str]
    ports: list[int]
    desired_replicas: Count
    stateful: bool
    confidence: Fraction


class TopologyEdge(Contract):
    caller: str
    dependency: str
    protocol: str
    provenance: str
    confidence: Fraction


class TopologySnapshot(Observed):
    graph_id: str
    upstream_sha: str | None
    generated_at: AwareDatetime
    sandbox_id: str
    namespace: str
    nodes: list[TopologyNode]
    edges: list[TopologyEdge]
    manual_corrections: list[str]


class VerifiedTarget(Contract):
    target_id: str
    sandbox_id: str
    namespace: str
    kind: str
    name: str
    uid: str
    selector: dict[str, str]
    allowed_fault_ids: list[str]
    allowed_template_ids: list[str]
    dependency_metadata: dict[str, str]
    route_metadata: dict[str, str]
    provenance: str


class Readiness(Contract):
    ready: Count | None
    desired: Count | None
    missing_reason: str | None = None


class SystemStateSnapshot(Observed):
    state_id: str
    captured_at: AwareDatetime
    valid_until: AwareDatetime
    sandbox_id: str
    ready_by_workload: dict[str, Readiness]
    request_rate: Positive | None
    error_rate: Fraction | None
    p95_ms: Positive | None
    db_connections_used: Count | None
    db_connections_limit: Count | None
    cpu_utilization: Fraction | None = None
    memory_utilization: Fraction | None = None
    missing_features: list[str]
    active_fault_id: str | None

    @model_validator(mode="after")
    def missing_has_reason(self):
        for name in ("request_rate", "error_rate", "p95_ms", "db_connections_used", "db_connections_limit", "cpu_utilization", "memory_utilization"):
            if getattr(self, name) is None and not any(x == name or x.startswith(name + ":") for x in self.missing_features):
                raise ValueError("Missing metric requires missing_features reason")
        if self.valid_until <= self.captured_at:
            raise ValueError("Invalid freshness interval")
        return self




class Prediction(Contract):
    duration_ms: Positive | None
    learner_version: str
    pre_update_sample_count: Count
    cold_start: bool
    drift_status: Literal["cold_start", "stable", "drift", "unknown"]
    quality_note: str




class FaultParameters(Contract):
    duration_seconds: Annotated[int, Field(ge=1, le=60)]
    latency_ms: Annotated[int, Field(ge=0, le=1000)] | None = None
    pod_uid: str | None = None
    proxy_name: str | None = None


class AbortLimits(Contract):
    error_rate: Fraction
    p95_ms: Annotated[float, Field(gt=0)]
    metric_max_age_seconds: Annotated[int, Field(ge=1, le=120)]
    recovery_timeout_seconds: Annotated[int, Field(ge=1, le=120)]
    minimum_samples: Annotated[int, Field(ge=1)]


class ExperimentProposal(Observed):
    proposal_id: str
    catalog_id: Literal["EXP-00", "EXP-01", "EXP-02", "EXP-03"]
    sandbox_id: str
    target_id: str
    fault_parameters: FaultParameters
    hypothesis: str
    reason: str
    expected_blast_radius: list[str]
    load_profile_id: str
    abort_limits: AbortLimits
    policy_version: str
    expires_at: AwareDatetime


class ApprovalRecord(Contract):
    approval_id: str
    proposal_hash: str
    actor_ref: str
    approved_at: AwareDatetime
    expires_at: AwareDatetime
    scope: Literal["experiment", "bounded_read", "action"]
    single_use: Literal[True] = True


class RunEvent(Observed):
    run_id: str
    sequence: Count
    timestamp: AwareDatetime
    event_type: str
    state: RunState
    details: dict[str, str | float | bool | None]


class LoadProfile(Contract):
    load_profile_id: str
    operation_mix: dict[str, Fraction]
    concurrency: Annotated[int, Field(ge=1)]
    requested_rate: Positive
    timeout_seconds: Positive
    warmup_seconds: Positive
    measurement_seconds: Positive
    generator_version: str


class OperationAggregate(Contract):
    attempted: Count
    succeeded: Count
    failed: Count
    error_categories: dict[str, Count]
    success_rate: Fraction | None
    p50_ms: Positive | None
    p95_ms: Positive | None
    percentile_method: str
    sample_count: Count
    missing: list[str]

    @model_validator(mode="after")
    def counts_match(self):
        if self.succeeded + self.failed != self.attempted:
            raise ValueError("Outcome counts disagree")
        if self.attempted and self.success_rate is not None and abs(self.success_rate - self.succeeded / self.attempted) > 1e-9:
            raise ValueError("Success rate disagrees with counts")
        return self


class PhaseSummary(Contract):
    phase: Literal["baseline", "fault", "recovery"]
    started_at: AwareDatetime
    ended_at: AwareDatetime
    operations: dict[str, OperationAggregate]
    offered_rate: Positive | None
    achieved_rate: Positive | None
    ready_min: Count | None
    desired_max: Count | None
    quality_notes: list[str]
    percentile_method: str

    @model_validator(mode="after")
    def ordered(self):
        if self.ended_at <= self.started_at:
            raise ValueError("Phase window must have positive duration")
        return self


class TimelineMarker(Contract):
    timestamp: AwareDatetime
    state: RunState
    reason: str


class ScoreComponents(Contract):
    availability: Annotated[float, Field(ge=0, le=100)] | None
    latency: Annotated[float, Field(ge=0, le=100)] | None
    recovery: Annotated[float, Field(ge=0, le=100)] | None
    safety: Annotated[float, Field(ge=0, le=100)] | None
    total: Annotated[float, Field(ge=0, le=100)] | None
    configuration: str
    missing: list[str]


class RunSummary(Observed):
    run_id: str
    catalog_id: str
    sandbox_id: str
    topology_hash: str
    application_revision: str | None
    load_profile: LoadProfile
    phase_summaries: list[PhaseSummary]
    timeline_markers: list[TimelineMarker]
    stop_reason: str | None
    cleanup_status: Cleanup
    recovery_status: Recovery
    recovery_seconds: Positive | None
    data_quality: list[str]
    score: ScoreComponents
    evidence_hash: str


class EvidenceRef(Contract):
    run_id: str
    evidence_hash: str
    pointer: str


class Finding(Contract):
    title: str
    explanation: str
    status: Literal["predicted", "supported_in_this_run", "not_supported", "insufficient_evidence"]
    priority: Annotated[int, Field(ge=1, le=5)]
    evidence_refs: list[EvidenceRef]


class AnalysisResult(Observed):
    run_id: str
    mode: Literal["live_model", "deterministic_fallback", "fixture"]
    measured_facts: list[str]
    hypotheses: list[Finding]
    alternatives: list[str]
    recommendations: list[Finding]
    evidence_refs: list[EvidenceRef]
    limitations: list[str]


class LearningFeatures(Contract):
    estimated_output_bytes: Positive | None
    select_operation: bool
    has_join: bool
    has_sort: bool
    p95_ms: Positive | None
    error_rate: Fraction | None
    connection_utilization: Fraction | None
    ready_fraction: Fraction | None


class LearningObservation(Observed):
    request_id: str
    captured_at: AwareDatetime
    state_id: str
    kind: Literal["arrival", "completed", "censored", "failed"]
    features: LearningFeatures | None
    duration_ms: Positive | None
    output_rows: Count | None
    active_fault_id: str | None


class LearningStatus(Observed):
    learner_version: str
    started_at: AwareDatetime
    processed: Count
    updates: Count
    pending_count: Annotated[int, Field(ge=0, le=128)]
    dropped_events: Count
    drift_status: Literal["cold_start", "stable", "drift", "unknown"]
    prediction_error: Positive | None
    retained_raw_examples: Literal[0] = 0
    queue_limit: Annotated[int, Field(ge=1, le=256)]
    pending_limit: Annotated[int, Field(ge=1, le=128)]
    metric_buffer_seconds: Annotated[int, Field(ge=1, le=120)]
    intent_ttl_seconds: Annotated[int, Field(ge=1, le=60)]
    model_limit_bytes: Count
    persistence: Literal["disabled"] = "disabled"


class ActionParameters(Contract):
    previous_replicas: Count
    desired_replicas: Annotated[int, Field(ge=1, le=10)]


class ActionProposal(Contract):
    action_id: str
    template_id: Literal["stateless_replicas"]
    sandbox_id: str
    target_id: str
    parameters: ActionParameters
    preconditions: list[str]
    rationale: str
    evidence_refs: list[EvidenceRef]
    observation_seconds: Annotated[int, Field(ge=1, le=120)]
    auto_revert_requested: bool


class ValidationCheck(Contract):
    name: str
    passed: bool
    reason: str


class ValidationResult(Contract):
    proposal_id: str
    proposal_hash: str
    valid: bool
    checks: list[ValidationCheck]
    rejection_codes: list[str]
    sandbox_id: str
    target_id: str
    policy_version: str
    expires_at: AwareDatetime


class RunHandle(Observed):
    run_id: str
    proposal_id: str
    state: RunState
    accepted_at: AwareDatetime
    status_url: str
    events_url: str


class StopReceipt(Observed):
    request_id: str
    accepted_at: AwareDatetime
    stop_latched: bool
    affected_run_ids: list[str]
    cleanup_status: Cleanup


class CleanupSummary(Observed):
    sandbox_id: str
    cleanup_status: Cleanup
    recovery_status: Recovery
    unresolved_run_ids: list[str]








class CatalogEntry(Contract):
    catalog_id: Literal["EXP-00", "EXP-01", "EXP-02", "EXP-03"]
    name: str
    mechanism: str
    available: bool
    max_duration_seconds: Annotated[int, Field(ge=1, le=60)]
    reason: str


class Catalog(Observed):
    entries: list[CatalogEntry]


class ErrorBody(Contract):
    code: str
    message: str
    retryable: bool
    correlation_id: str
    details: dict[str, str]


class RunStatus(Observed):
    handle: RunHandle
    cleanup_status: Cleanup
    recovery_status: Recovery
    summary: RunSummary | None


class Health(Observed):
    status: Literal["ready", "degraded"]
    dependencies: dict[str, bool]
    sandbox_id: str
    context: str
    namespace: str
    mutations_enabled: bool
    sql_execution_enabled: bool
    adaptive_rollback_enabled: Literal[False] = False
    limitations: list[str]
