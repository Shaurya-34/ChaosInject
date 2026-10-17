"""Provisional telemetry contract models.

**Ownership note.** ``backend/app/contracts/`` (Sachin) is the authoritative home
for shared Pydantic definitions. These classes exist so the telemetry workstream
can be built and tested before that integration commit lands. Field names,
types, units and enum members follow ``02_SHARED_INTERFACES.md`` exactly, so the
swap is mechanical: once ``backend.app.contracts`` exports these names,
``_contracts.py`` picks them up and this module is deleted.

Do not add telemetry-only fields here. If telemetry needs a new exported field,
raise it with Sachin so it lands in contracts/v1 rather than diverging.

Conventions enforced below, from the shared serialisation rules:

* every exported object carries ``schema_version``
* observed objects carry ``source`` (``live`` | ``fixture`` | ``replay``)
* unknown values are ``None`` *with a recorded reason*, never ``0``
* NaN and infinity are rejected
* unexpected fields are rejected
* units are explicit in the field name (``_ms``, ``_seconds``, ``_bytes``)
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import SCHEMA_VERSION

# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------


def new_id(prefix: str) -> str:
    """Random correlation ID. Carries no information about the subject."""
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _reject_nonfinite(value: float | None) -> float | None:
    if value is None:
        return None
    if math.isnan(value) or math.isinf(value):
        raise ValueError("NaN and infinity are not valid telemetry values")
    return value


Finite = Annotated[float, Field(allow_inf_nan=False)]


class Source(str, Enum):
    LIVE = "live"
    FIXTURE = "fixture"
    REPLAY = "replay"


class Phase(str, Enum):
    BASELINE = "baseline"
    FAULT = "fault"
    RECOVERY = "recovery"


class CleanupStatus(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"


class RecoveryStatus(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN = "unknown"
    PENDING = "pending"


class Outcome(str, Enum):
    """Per-attempt classification.

    ``TIMEOUT`` is deliberately distinct from ``ERROR``: a timed-out attempt has
    no completed duration, so it must not enter the success latency population.
    ``SEMANTIC_FAILURE`` is an HTTP 2xx response whose body did not show the
    expected result -- a rendered error page is not a successful transaction.
    """

    SUCCESS = "success"
    SEMANTIC_FAILURE = "semantic_failure"
    HTTP_ERROR = "http_error"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    SKIPPED = "skipped"


SUCCESS_OUTCOMES = frozenset({Outcome.SUCCESS})


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class Exported(Base):
    schema_version: Literal["1.0"] = SCHEMA_VERSION


# --------------------------------------------------------------------------
# workload
# --------------------------------------------------------------------------


class LoadProfile(Exported):
    """Exact workload settings a phase was measured under.

    Comparing two phases measured under different profiles is invalid, so the
    profile travels with every summary and the scorer refuses mismatches.
    """

    load_profile_id: str
    generator_version: str
    operation_mix: dict[str, Finite]
    concurrency: int = Field(ge=1)
    requested_rate_rps: Finite = Field(gt=0)
    request_timeout_seconds: Finite = Field(gt=0)
    warmup_seconds: Finite = Field(ge=0)
    measurement_window_seconds: Finite | None = None
    endpoint_identity: str | None = Field(
        default=None,
        description="Which endpoint was driven, e.g. 'frontend via port-forward 127.0.0.1:8080'.",
    )

    @field_validator("operation_mix")
    @classmethod
    def _mix_sums_to_one(cls, mix: dict[str, float]) -> dict[str, float]:
        if not mix:
            raise ValueError("operation_mix must not be empty")
        total = sum(mix.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"operation_mix must sum to 1.0, got {total}")
        if any(v < 0 for v in mix.values()):
            raise ValueError("operation_mix weights must be non-negative")
        return mix


# --------------------------------------------------------------------------
# aggregates
# --------------------------------------------------------------------------


class OperationAggregate(Exported):
    """Per-operation numbers for one phase.

    ``p50_ms`` / ``p95_ms`` are computed over the **success-only** population --
    stated explicitly in ``latency_population`` because mixing timed-out and
    failed attempts into a latency distribution produces a meaningless number.
    Both are ``None`` when ``sample_count`` is below the configured minimum, with
    the reason recorded in ``missing``.
    """

    operation: str
    attempted: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    timed_out: int = Field(ge=0)
    error_categories: dict[str, int] = Field(default_factory=dict)
    success_rate: Finite | None = Field(default=None, ge=0.0, le=1.0)
    p50_ms: Finite | None = Field(default=None, ge=0.0)
    p95_ms: Finite | None = Field(default=None, ge=0.0)
    percentile_method: str
    latency_population: Literal["success_only", "all_attempts"] = "success_only"
    sample_count: int = Field(ge=0)
    missing: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _counts_reconcile(self) -> "OperationAggregate":
        if self.succeeded + self.failed + self.timed_out != self.attempted:
            raise ValueError(
                "attempted must equal succeeded + failed + timed_out "
                f"({self.attempted} != {self.succeeded}+{self.failed}+{self.timed_out})"
            )
        if self.attempted == 0 and self.success_rate is not None:
            raise ValueError("success_rate must be None when nothing was attempted")
        return self


class ReadyState(Exported):
    """Replica readiness as numbers, never a '11/11' string."""

    ready: int = Field(ge=0)
    desired: int = Field(ge=0)


class PhaseSummary(Exported):
    """Immutable aggregate for one measurement phase."""

    phase: Phase
    source: Source
    started_at: datetime
    ended_at: datetime
    duration_seconds: Finite = Field(ge=0)
    load_profile: LoadProfile
    operations: dict[str, OperationAggregate]
    overall: OperationAggregate
    achieved_rate_rps: Finite | None = Field(default=None, ge=0)
    offered_rate_rps: Finite | None = Field(default=None, ge=0)
    ready_min: dict[str, ReadyState] = Field(default_factory=dict)
    ready_max: dict[str, ReadyState] = Field(default_factory=dict)
    percentile_method: str
    quality_notes: list[str] = Field(default_factory=list)
    clock_uncertainty: str | None = None


# --------------------------------------------------------------------------
# system state
# --------------------------------------------------------------------------


class SystemStateSnapshot(Exported):
    """Point-in-time view used by the stop policy and by the learner.

    ``valid_until`` is the hard freshness boundary. A consumer that reads this
    snapshot after ``valid_until`` must treat it as **unavailable**, not healthy.
    Any metric we could not obtain is absent from the numeric fields and named
    in ``missing_features`` -- a null here means unknown, never zero.
    """

    state_id: str = Field(default_factory=lambda: new_id("state"))
    source: Source
    captured_at: datetime
    valid_until: datetime
    sandbox_id: str
    ready_by_workload: dict[str, ReadyState] = Field(default_factory=dict)
    request_rate: Finite | None = Field(default=None, ge=0)
    error_rate: Finite | None = Field(default=None, ge=0.0, le=1.0)
    p95_ms: Finite | None = Field(default=None, ge=0)
    db_connections_used: int | None = Field(default=None, ge=0)
    db_connections_limit: int | None = Field(default=None, ge=0)
    cpu_utilization: Finite | None = Field(default=None, ge=0.0)
    memory_utilization: Finite | None = Field(default=None, ge=0.0)
    missing_features: list[str] = Field(default_factory=list)
    active_fault_id: str | None = None
    sample_count: int = Field(default=0, ge=0)

    def is_fresh(self, *, at: datetime) -> bool:
        return at <= self.valid_until

    def freshness_state(self, *, at: datetime) -> Literal["fresh", "stale"]:
        return "fresh" if self.is_fresh(at=at) else "stale"

    @property
    def db_connection_utilization(self) -> float | None:
        """Fraction of the connection limit in use, or None if either side is unknown."""
        if self.db_connections_used is None or not self.db_connections_limit:
            return None
        return self.db_connections_used / self.db_connections_limit


# --------------------------------------------------------------------------
# plan estimation
# --------------------------------------------------------------------------


class PlanEstimate(Exported):
    """Sanitised numeric summary of a PostgreSQL plan.

    Contains **no** SQL, no bound parameters and no raw plan expressions. The
    source statement, its parameters and the raw plan JSON are transient and
    discarded once this object is built.

    ``planner_cost`` is PostgreSQL's abstract cost unit. It is **not** a duration
    and must never be presented as milliseconds.

    ``max_node_estimated_rows`` exists because a top-level aggregate can return
    one row while scanning millions; resource risk follows the largest scanned
    node, not the output row count.
    """

    request_id: str
    source: Source
    captured_at: datetime
    plan_source: Literal["explain_json", "unavailable"]
    estimated_rows: Finite | None = Field(default=None, ge=0)
    estimated_width_bytes: Finite | None = Field(default=None, ge=0)
    estimated_output_bytes: Finite | None = Field(default=None, ge=0)
    planner_cost: Finite | None = Field(default=None, ge=0)
    max_node_estimated_rows: Finite | None = Field(default=None, ge=0)
    node_categories: list[str] = Field(default_factory=list)
    stats_quality: Literal["fresh", "stale", "unknown"] = "unknown"
    uncertainty: list[str] = Field(default_factory=list)
    estimation_error: str | None = Field(
        default=None,
        description="Redacted failure class only; never driver text or statement content.",
    )

    @model_validator(mode="after")
    def _unavailable_has_no_numbers(self) -> "PlanEstimate":
        if self.plan_source == "unavailable":
            numeric = (
                self.estimated_rows,
                self.estimated_width_bytes,
                self.estimated_output_bytes,
                self.planner_cost,
                self.max_node_estimated_rows,
            )
            if any(v is not None for v in numeric):
                raise ValueError(
                    "an unavailable plan estimate must not carry numeric estimates"
                )
            if not self.uncertainty:
                raise ValueError("an unavailable plan estimate must record a reason")
        return self


# --------------------------------------------------------------------------
# learning channel
# --------------------------------------------------------------------------


class LearningKind(str, Enum):
    ARRIVAL = "arrival"
    COMPLETED = "completed"
    CENSORED = "censored"
    FAILED = "failed"


class LearningObservation(Exported):
    """One transient observation handed to the learner. RAM only.

    ``features`` are fixed at **arrival**, from the state at decision time.
    Outcome events carry no features; they are joined to the pending arrival by
    ``request_id``. Substituting post-execution state into a training record
    would leak the outcome into its own inputs.

    ``CENSORED`` marks a cancelled or capped job: the full-query duration was
    never observed, so it is a lower bound and not a completed sample. A blocked
    query produces no duration at all.
    """

    request_id: str
    source: Source
    captured_at: datetime
    state_id: str | None = None
    kind: LearningKind
    features: dict[str, Finite] | None = None
    duration_ms: Finite | None = Field(default=None, ge=0)
    rows_returned: int | None = Field(default=None, ge=0)
    bytes_returned: int | None = Field(default=None, ge=0)
    active_fault_id: str | None = None
    baseline_context: Literal["baseline", "injected_fault", "unknown"] = "unknown"
    confounders: list[str] = Field(default_factory=list)
    censoring_reason: str | None = None

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> "LearningObservation":
        if self.kind is LearningKind.ARRIVAL:
            if self.features is None:
                raise ValueError("an arrival observation must carry its features")
            if self.duration_ms is not None:
                raise ValueError("an arrival observation has no observed duration")
        else:
            if self.features is not None:
                raise ValueError(
                    "outcome observations must not restate features; join on request_id"
                )
        if self.kind is LearningKind.CENSORED and self.censoring_reason is None:
            raise ValueError("a censored observation must record why it was censored")
        if self.kind is LearningKind.FAILED and self.duration_ms is not None:
            raise ValueError(
                "a failed query has no valid completed duration to learn from"
            )
        return self


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


class ScoreComponent(Exported):
    """One scoring component, or an explicit reason it is unknown."""

    name: Literal["availability", "latency", "recovery", "safety"]
    value: Finite | None = Field(default=None, ge=0.0, le=100.0)
    weight: Finite = Field(gt=0, le=1)
    known: bool
    reason: str | None = None
    inputs: dict[str, Finite | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unknown_has_reason(self) -> "ScoreComponent":
        if self.known and self.value is None:
            raise ValueError("a known component must carry a value")
        if not self.known:
            if self.value is not None:
                raise ValueError("an unknown component must not carry a value")
            if not self.reason:
                raise ValueError("an unknown component must record a reason")
        return self


class ResilienceScore(Exported):
    """Deterministic resilience score, or an explicit insufficiency.

    A total is produced only when every weighted component is known. Weights are
    never renormalised to paper over a missing component, and a missing value is
    never silently treated as zero -- both would turn absent evidence into a
    reassuring number.

    This is a team-defined hackathon measure. It is not an official Bosch score,
    not an SLO, and not a probability of failure.
    """

    run_id: str
    source: Source
    calculation_version: str
    components: list[ScoreComponent]
    total: Finite | None = Field(default=None, ge=0.0, le=100.0)
    insufficient_evidence: bool
    guardrails: list[str] = Field(default_factory=list)
    evidence_hash: str | None = None

    @model_validator(mode="after")
    def _total_consistent(self) -> "ResilienceScore":
        if self.insufficient_evidence and self.total is not None:
            raise ValueError("insufficient evidence must not produce a total")
        if not self.insufficient_evidence and self.total is None:
            raise ValueError("a sufficient score must produce a total")
        return self


# --------------------------------------------------------------------------
# run summary
# --------------------------------------------------------------------------


class TimelineMarker(Exported):
    sequence: int = Field(ge=0)
    timestamp: datetime
    event_type: str
    detail: str | None = None


class DataQuality(Exported):
    sufficient_samples: bool
    percentile_method: str
    min_samples_for_percentile: int = Field(ge=1)
    notes: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    dropped_samples: int = Field(default=0, ge=0)
    clock_uncertainty: str | None = None


class RunSummary(Exported):
    """Immutable sanitised evidence for one experiment run.

    Contains aggregates only -- no raw events, no request bodies, no source
    statements. ``evidence_hash`` covers every field except itself, so a report
    value and a UI value can be proven to come from the same summary.
    """

    run_id: str
    source: Source
    catalog_id: str
    sandbox_id: str
    topology_hash: str | None = None
    created_at: datetime
    load_profile: LoadProfile
    phase_summaries: dict[str, PhaseSummary]
    timeline_markers: list[TimelineMarker] = Field(default_factory=list)
    stop_reason: str | None = None
    cleanup_status: CleanupStatus
    recovery_status: RecoveryStatus
    recovery_seconds: Finite | None = Field(default=None, ge=0)
    data_quality: DataQuality
    evidence_hash: str


class EvidenceRef(Exported):
    """Pointer into a specific field of a specific summary.

    Every numeric claim in an AI analysis or in the findings report carries one
    of these, so any figure can be traced back to the measurement that produced
    it.
    """

    run_id: str
    evidence_hash: str
    pointer: str = Field(description="JSON Pointer (RFC 6901) into the RunSummary.")

    @field_validator("pointer")
    @classmethod
    def _looks_like_pointer(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("JSON Pointer must start with '/'")
        return value


__all__ = [
    "Base",
    "CleanupStatus",
    "DataQuality",
    "EvidenceRef",
    "Exported",
    "LearningKind",
    "LearningObservation",
    "LoadProfile",
    "OperationAggregate",
    "Outcome",
    "Phase",
    "PhaseSummary",
    "PlanEstimate",
    "ReadyState",
    "RecoveryStatus",
    "ResilienceScore",
    "RunSummary",
    "ScoreComponent",
    "Source",
    "SystemStateSnapshot",
    "TimelineMarker",
    "new_id",
]
