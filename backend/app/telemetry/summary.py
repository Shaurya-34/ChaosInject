"""Immutable run summaries and evidence references.

``finalize_run`` is the last thing telemetry does for an experiment. It takes the
closed phase summaries plus the runtime facts Tooshita supplies (stop reason,
cleanup status) and freezes them into a ``RunSummary`` with a content hash.

The hash matters more than it looks. The findings report, the dashboard and the
AI analysis all quote numbers; the hash lets anyone prove that three quoted
figures came from the same measurement rather than from three different runs
that happened to be open in three different tabs. ``EvidenceRef`` then points at
a specific field inside a specific hashed summary, so every claim in the report
is traceable to the measurement behind it.

What a summary deliberately does **not** contain: raw events, request bodies,
cookies, statement text, or result rows. Aggregates only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ._contracts import (
    CleanupStatus,
    DataQuality,
    EvidenceRef,
    LoadProfile,
    PhaseSummary,
    RecoveryStatus,
    RunSummary,
    Source,
    TimelineMarker,
)
from .clock import ClockUncertainty, now_utc
from .config import SamplingConfig
from .recovery import RecoveryOutcome


@dataclass(frozen=True)
class RuntimeFacts:
    """What the runtime owns and telemetry must not infer.

    Telemetry cannot know whether a rollback succeeded -- it did not perform one.
    Guessing from metrics would let a recovered-looking application mask a failed
    cleanup, which is precisely the state that must stay loud.
    """

    catalog_id: str
    sandbox_id: str
    stop_reason: str | None
    cleanup_status: CleanupStatus
    topology_hash: str | None = None
    markers: tuple[TimelineMarker, ...] = ()


def canonical_json(payload: dict[str, Any]) -> str:
    """Stable JSON for hashing: sorted keys, no incidental whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_evidence_hash(payload: dict[str, Any]) -> str:
    """SHA-256 over everything except the hash field itself."""
    body = {k: v for k, v in payload.items() if k != "evidence_hash"}
    return "sha256:" + hashlib.sha256(canonical_json(body).encode()).hexdigest()


def finalize_run(
    run_id: str,
    phase_summaries: dict[str, PhaseSummary],
    load_profile: LoadProfile,
    facts: RuntimeFacts,
    recovery: RecoveryOutcome | None,
    *,
    sampling: SamplingConfig | None = None,
    dropped_samples: int = 0,
    clock_uncertainty: ClockUncertainty | None = None,
    source: Source = Source.LIVE,
    extra_notes: tuple[str, ...] = (),
) -> RunSummary:
    """Freeze a run into its final sanitised evidence object."""
    cfg = sampling or SamplingConfig()
    uncertainty = clock_uncertainty or ClockUncertainty()

    notes: list[str] = list(extra_notes)
    missing: list[str] = []

    for name, summary in phase_summaries.items():
        for item in summary.overall.missing:
            missing.append(f"{name}: {item}")
        notes.extend(f"{name}: {note}" for note in summary.quality_notes)

    sufficient = all(
        summary.overall.sample_count >= cfg.min_samples_for_percentile
        for summary in phase_summaries.values()
    )
    if not sufficient:
        notes.append(
            "at least one phase has fewer successful samples than the configured "
            f"minimum of {cfg.min_samples_for_percentile}; percentile fields are null "
            "for those phases and dependent score components are unknown"
        )

    if recovery is not None:
        notes.append(f"recovery: {recovery.reason}")

    quality = DataQuality(
        sufficient_samples=sufficient,
        percentile_method=(
            next(iter(phase_summaries.values())).percentile_method
            if phase_summaries
            else "n/a"
        ),
        min_samples_for_percentile=cfg.min_samples_for_percentile,
        notes=notes,
        missing=missing,
        dropped_samples=dropped_samples,
        clock_uncertainty=uncertainty.describe(),
    )

    recovery_status = recovery.status if recovery else RecoveryStatus.UNKNOWN
    recovery_seconds = recovery.recovery_seconds if recovery else None

    draft = RunSummary(
        run_id=run_id,
        source=source,
        catalog_id=facts.catalog_id,
        sandbox_id=facts.sandbox_id,
        topology_hash=facts.topology_hash,
        created_at=now_utc(),
        load_profile=load_profile,
        phase_summaries=phase_summaries,
        timeline_markers=list(facts.markers),
        stop_reason=facts.stop_reason,
        cleanup_status=facts.cleanup_status,
        recovery_status=recovery_status,
        recovery_seconds=recovery_seconds,
        data_quality=quality,
        evidence_hash="pending",
    )

    payload = draft.model_dump(mode="json")
    digest = compute_evidence_hash(payload)
    return draft.model_copy(update={"evidence_hash": digest})


def verify_evidence_hash(summary: RunSummary) -> bool:
    """Re-derive the hash. Detects a summary that was altered after freezing."""
    payload = summary.model_dump(mode="json")
    return compute_evidence_hash(payload) == summary.evidence_hash


def evidence_ref(summary: RunSummary, pointer: str) -> EvidenceRef:
    """Build a pointer into a summary, verifying the field exists.

    Resolving the pointer here means a citation in the findings report cannot
    reference a field that was never measured.
    """
    resolve_pointer(summary, pointer)
    return EvidenceRef(
        run_id=summary.run_id,
        evidence_hash=summary.evidence_hash,
        pointer=pointer,
    )


def resolve_pointer(summary: RunSummary, pointer: str) -> Any:
    """Minimal RFC 6901 JSON Pointer resolution against a summary."""
    if not pointer.startswith("/"):
        raise ValueError("JSON Pointer must start with '/'")

    current: Any = summary.model_dump(mode="json")
    for token in pointer.lstrip("/").split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if key not in current:
                raise KeyError(f"pointer {pointer!r} does not resolve: missing {key!r}")
            current = current[key]
        elif isinstance(current, list):
            try:
                current = current[int(key)]
            except (ValueError, IndexError) as exc:
                raise KeyError(f"pointer {pointer!r} does not resolve at {key!r}") from exc
        else:
            raise KeyError(f"pointer {pointer!r} descends into a scalar at {key!r}")
    return current
