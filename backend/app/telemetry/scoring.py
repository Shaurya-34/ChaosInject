"""Deterministic resilience scoring.

Formula, fixed by the shared interface contract:

    score = 0.40*A + 0.20*L + 0.25*R + 0.15*S

    A = 100 * fault_successes / fault_attempts
    L = 100 * clip((latency_abort_ms - fault_p95_ms)
                   / (latency_abort_ms - baseline_p95_ms), 0, 1)
    R = 100 * clip(1 - recovery_seconds / recovery_timeout_seconds, 0, 1)
    S = 100 if workflow and cleanup/recovery checks passed, else 0

Everything interesting about this module is what it *refuses* to do.

* It does not renormalise weights when a component is unknown. Dropping latency
  and rescaling the rest to 100 would turn "we could not measure latency" into a
  higher score.
* It does not treat a missing value as zero, and it does not treat it as full
  marks either. Unknown propagates to ``insufficient_evidence`` and no total.
* It distinguishes *observed failure to recover* (R = 0, we watched and it did
  not come back) from *lost observation* (R unknown, we stopped watching).
* It refuses to compare phases measured under different load profiles.
* It requires the configured minimum sample count before trusting a percentile.

Components are always returned, even when the total cannot be. A reviewer can
see exactly which inputs were available and which were not.

This is a team-defined hackathon measure. It is not an official Bosch score, not
an SLO, and not a probability of failure.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._contracts import (
    CleanupStatus,
    Phase,
    PhaseSummary,
    RecoveryStatus,
    ResilienceScore,
    ScoreComponent,
    Source,
)
from .config import SCORING_VERSION, SamplingConfig

WEIGHTS: dict[str, float] = {
    "availability": 0.40,
    "latency": 0.20,
    "recovery": 0.25,
    "safety": 0.15,
}


def clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class SafetyChecks:
    """Inputs to the safety component.

    ``S`` is deliberately binary. A partially-completed rollback is not 70% safe;
    it is an incident that needs an operator, and a fractional score would let it
    disappear into an average.
    """

    cleanup_status: CleanupStatus
    recovery_status: RecoveryStatus
    workflow_constraints_held: bool
    notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        if not self.workflow_constraints_held:
            return False
        if self.cleanup_status not in (
            CleanupStatus.VERIFIED,
            CleanupStatus.NOT_APPLICABLE,
        ):
            return False
        return self.recovery_status is RecoveryStatus.VERIFIED


@dataclass(frozen=True)
class ScoringInputs:
    run_id: str
    baseline: PhaseSummary
    fault: PhaseSummary
    latency_abort_ms: float
    recovery_timeout_seconds: float
    recovery_status: RecoveryStatus
    recovery_seconds: float | None
    safety: SafetyChecks
    evidence_hash: str | None = None
    source: Source = Source.LIVE


class ResilienceScorer:
    def __init__(self, sampling: SamplingConfig | None = None) -> None:
        self._sampling = sampling or SamplingConfig()

    # -- components --------------------------------------------------------

    def _availability(self, inputs: ScoringInputs) -> ScoreComponent:
        overall = inputs.fault.overall
        attempts = overall.attempted
        successes = overall.succeeded
        base = dict(fault_attempts=float(attempts), fault_successes=float(successes))

        if attempts <= 0:
            return ScoreComponent(
                name="availability",
                weight=WEIGHTS["availability"],
                known=False,
                reason=(
                    "no attempts were recorded during the fault window; availability "
                    "is unknown, not zero"
                ),
                inputs=base,
            )

        return ScoreComponent(
            name="availability",
            value=round(100.0 * successes / attempts, 3),
            weight=WEIGHTS["availability"],
            known=True,
            inputs=base,
        )

    def _latency(self, inputs: ScoringInputs) -> ScoreComponent:
        baseline_p95 = inputs.baseline.overall.p95_ms
        fault_p95 = inputs.fault.overall.p95_ms
        abort = inputs.latency_abort_ms
        base: dict[str, float | None] = {
            "baseline_p95_ms": baseline_p95,
            "fault_p95_ms": fault_p95,
            "latency_abort_ms": abort,
        }

        minimum = self._sampling.min_samples_for_percentile
        if baseline_p95 is None:
            return ScoreComponent(
                name="latency",
                weight=WEIGHTS["latency"],
                known=False,
                reason=(
                    "baseline p95 unavailable "
                    f"({inputs.baseline.overall.sample_count} successful samples, "
                    f"minimum {minimum})"
                ),
                inputs=base,
            )
        if fault_p95 is None:
            return ScoreComponent(
                name="latency",
                weight=WEIGHTS["latency"],
                known=False,
                reason=(
                    "fault-window p95 unavailable "
                    f"({inputs.fault.overall.sample_count} successful samples, "
                    f"minimum {minimum}); a near-total outage can legitimately produce "
                    "too few successes to measure latency"
                ),
                inputs=base,
            )
        if abort <= baseline_p95:
            return ScoreComponent(
                name="latency",
                weight=WEIGHTS["latency"],
                known=False,
                reason=(
                    f"latency_abort_ms ({abort:.0f}) must exceed baseline p95 "
                    f"({baseline_p95:.0f}); the normalisation range is degenerate"
                ),
                inputs=base,
            )

        ratio = (abort - fault_p95) / (abort - baseline_p95)
        return ScoreComponent(
            name="latency",
            value=round(100.0 * clip(ratio, 0.0, 1.0), 3),
            weight=WEIGHTS["latency"],
            known=True,
            inputs=base,
        )

    def _recovery(self, inputs: ScoringInputs) -> ScoreComponent:
        base: dict[str, float | None] = {
            "recovery_seconds": inputs.recovery_seconds,
            "recovery_timeout_seconds": inputs.recovery_timeout_seconds,
        }
        status = inputs.recovery_status

        if status is RecoveryStatus.FAILED:
            # Observed: we watched for the full timeout and it did not recover.
            return ScoreComponent(
                name="recovery",
                value=0.0,
                weight=WEIGHTS["recovery"],
                known=True,
                inputs=base,
            )

        if status in (RecoveryStatus.UNKNOWN, RecoveryStatus.PENDING):
            return ScoreComponent(
                name="recovery",
                weight=WEIGHTS["recovery"],
                known=False,
                reason=(
                    f"recovery status is {status.value}; observation was lost or "
                    "incomplete, which is not the same as a failure to recover"
                ),
                inputs=base,
            )

        if inputs.recovery_seconds is None:
            return ScoreComponent(
                name="recovery",
                weight=WEIGHTS["recovery"],
                known=False,
                reason="recovery reported verified but no recovery time was recorded",
                inputs=base,
            )
        if inputs.recovery_timeout_seconds <= 0:
            return ScoreComponent(
                name="recovery",
                weight=WEIGHTS["recovery"],
                known=False,
                reason="recovery_timeout_seconds must be positive",
                inputs=base,
            )

        ratio = 1.0 - (inputs.recovery_seconds / inputs.recovery_timeout_seconds)
        return ScoreComponent(
            name="recovery",
            value=round(100.0 * clip(ratio, 0.0, 1.0), 3),
            weight=WEIGHTS["recovery"],
            known=True,
            inputs=base,
        )

    def _safety(self, inputs: ScoringInputs) -> ScoreComponent:
        checks = inputs.safety
        return ScoreComponent(
            name="safety",
            value=100.0 if checks.passed else 0.0,
            weight=WEIGHTS["safety"],
            known=True,
            inputs={
                "workflow_constraints_held": float(checks.workflow_constraints_held),
                "cleanup_verified": float(
                    checks.cleanup_status
                    in (CleanupStatus.VERIFIED, CleanupStatus.NOT_APPLICABLE)
                ),
                "recovery_verified": float(
                    checks.recovery_status is RecoveryStatus.VERIFIED
                ),
            },
        )

    # -- guardrails --------------------------------------------------------

    def _guardrails(self, inputs: ScoringInputs) -> list[str]:
        issues: list[str] = []

        if inputs.baseline.phase is not Phase.BASELINE:
            issues.append("baseline summary is not a baseline phase")
        if inputs.fault.phase is not Phase.FAULT:
            issues.append("fault summary is not a fault phase")

        b, f = inputs.baseline.load_profile, inputs.fault.load_profile
        if b.load_profile_id != f.load_profile_id:
            issues.append(
                f"load profiles differ ({b.load_profile_id} vs {f.load_profile_id}); "
                "phases measured under different workloads are not comparable"
            )
        elif b.requested_rate_rps != f.requested_rate_rps or b.concurrency != f.concurrency:
            issues.append(
                "load profile settings differ between phases despite a shared ID"
            )

        baseline_rate = inputs.baseline.overall.success_rate
        if baseline_rate is None:
            issues.append("baseline success rate unavailable; baseline health unproven")
        elif baseline_rate < 0.95:
            issues.append(
                f"baseline success rate {baseline_rate:.3f} is below 0.95; the system "
                "was not healthy before injection, so the comparison is unsound"
            )

        if inputs.baseline.overall.attempted == 0:
            issues.append("baseline window recorded no attempts")

        return issues

    # -- entry point -------------------------------------------------------

    def score(self, inputs: ScoringInputs) -> ResilienceScore:
        guardrails = self._guardrails(inputs)
        components = [
            self._availability(inputs),
            self._latency(inputs),
            self._recovery(inputs),
            self._safety(inputs),
        ]

        blocking = [g for g in guardrails if "not comparable" in g or "unsound" in g]
        all_known = all(c.known for c in components)

        if not all_known or blocking:
            if blocking:
                guardrails = guardrails + [
                    "no total produced: a blocking guardrail failed"
                ]
            else:
                unknown = ", ".join(c.name for c in components if not c.known)
                guardrails = guardrails + [
                    f"no total produced: unknown component(s) {unknown}; weights are "
                    "not renormalised and missing values are not treated as zero"
                ]
            return ResilienceScore(
                run_id=inputs.run_id,
                source=inputs.source,
                calculation_version=SCORING_VERSION,
                components=components,
                total=None,
                insufficient_evidence=True,
                guardrails=guardrails,
                evidence_hash=inputs.evidence_hash,
            )

        total = sum((c.value or 0.0) * c.weight for c in components)
        return ResilienceScore(
            run_id=inputs.run_id,
            source=inputs.source,
            calculation_version=SCORING_VERSION,
            components=components,
            total=round(total, 2),
            insufficient_evidence=False,
            guardrails=guardrails,
            evidence_hash=inputs.evidence_hash,
        )


@dataclass(frozen=True)
class AggregateScore:
    """Across several experiments.

    Equal weights, and only when every included result is valid. If any run is
    insufficient, no aggregate is produced and coverage is reported instead --
    averaging the runs that happened to succeed would quietly select for the
    experiments that went well.
    """

    mean_total: float | None
    included_runs: tuple[str, ...]
    excluded_runs: tuple[str, ...]
    complete_coverage: bool
    note: str


def aggregate(scores: list[ResilienceScore]) -> AggregateScore:
    included = [s for s in scores if not s.insufficient_evidence and s.total is not None]
    excluded = [s for s in scores if s.insufficient_evidence or s.total is None]

    if not scores:
        return AggregateScore(None, (), (), False, "no experiments scored")

    if excluded:
        return AggregateScore(
            mean_total=None,
            included_runs=tuple(s.run_id for s in included),
            excluded_runs=tuple(s.run_id for s in excluded),
            complete_coverage=False,
            note=(
                f"{len(excluded)} of {len(scores)} experiments lack sufficient evidence; "
                "no aggregate is reported. Component scores remain available per run."
            ),
        )

    mean = sum(s.total or 0.0 for s in included) / len(included)
    return AggregateScore(
        mean_total=round(mean, 2),
        included_runs=tuple(s.run_id for s in included),
        excluded_runs=(),
        complete_coverage=True,
        note=f"equal-weight mean across {len(included)} valid experiments",
    )
