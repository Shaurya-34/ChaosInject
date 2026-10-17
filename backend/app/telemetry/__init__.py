"""Telemetry workstream (Navya).

Measurement, live state and deterministic scoring for
Bosch Resilience AI.

What this package guarantees to the rest of the system:

* **Missing telemetry is missing, not healthy.** Unknown values are ``None``
  with a recorded reason. Nothing here ever reports an unmeasured metric as
  zero, and a stale snapshot is distinguishable from a fresh one.
* **HTTP 200 is not success.** The probe verifies a semantic marker before
  counting a transaction, so a rendered error page is a failure.
* **Percentiles are merged, never averaged.** Window aggregation goes through a
  mergeable histogram; the method and its approximation are exported on every
  aggregate.
* **Recovery means business recovery.** Restored replicas alone do not qualify;
  critical transactions must succeed for a sustained interval.
* **The learning channel retains nothing.** Bounded in RAM, no persistence, with
  drop counters reported.
* **Telemetry observes and never mutates.** No function in this package changes
  cluster or database state.

Entry points: ``LiveTelemetryPort`` for production, ``FakeTelemetryPort`` for
every other workstream to build against.
"""

from ._contracts import (  # noqa: F401
    CleanupStatus,
    DataQuality,
    EvidenceRef,
    LearningKind,
    LearningObservation,
    LoadProfile,
    OperationAggregate,
    Outcome,
    Phase,
    PhaseSummary,
    ReadyState,
    RecoveryStatus,
    ResilienceScore,
    RunSummary,
    ScoreComponent,
    Source,
    SystemStateSnapshot,
    TimelineMarker,
)
from .config import DEFAULT_CONFIG, SCHEMA_VERSION, SCORING_VERSION, TelemetryConfig
from .learning import LearningChannel
from .port import LiveTelemetryPort, TelemetryPort
from .recovery import FrozenBaseline, RecoveryDetector, RecoveryOutcome
from .scoring import ResilienceScorer, SafetyChecks, ScoringInputs, aggregate
from .state import state_is_usable_for_safety
from .summary import RuntimeFacts, evidence_ref, finalize_run, verify_evidence_hash

__version__ = "1.0.0"

__all__ = [
    "DEFAULT_CONFIG",
    "SCHEMA_VERSION",
    "SCORING_VERSION",
    "CleanupStatus",
    "DataQuality",
    "EvidenceRef",
    "FrozenBaseline",
    "LearningChannel",
    "LearningKind",
    "LearningObservation",
    "LiveTelemetryPort",
    "LoadProfile",
    "OperationAggregate",
    "Outcome",
    "Phase",
    "PhaseSummary",
    "ReadyState",
    "RecoveryDetector",
    "RecoveryOutcome",
    "RecoveryStatus",
    "ResilienceScore",
    "ResilienceScorer",
    "RunSummary",
    "RuntimeFacts",
    "SafetyChecks",
    "ScoreComponent",
    "ScoringInputs",
    "Source",
    "SystemStateSnapshot",
    "TelemetryConfig",
    "TelemetryPort",
    "TimelineMarker",
    "__version__",
    "aggregate",
    "evidence_ref",
    "finalize_run",
    "state_is_usable_for_safety",
    "verify_evidence_hash",
]
