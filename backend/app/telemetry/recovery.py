"""Sustained recovery detection.

The question "has it recovered?" has three honest answers, and this module is
careful to keep them apart:

``verified``
    Infrastructure is healthy **and** business-critical transactions have
    succeeded for a sustained interval. A restored replica count alone does not
    qualify: a Ready pod that still cannot complete a checkout has not recovered.

``failed``
    We watched for the full timeout and the system did not return to health.
    This is an observation, and it scores zero recovery credit.

``unknown``
    We stopped watching, lost telemetry, or never had enough samples. This is
    *not* a failure, and it must not be scored as one. Absence of evidence gets
    reported as absence of evidence.

The baseline used for comparison is the one **frozen before injection**. Feeding
degraded measurements back into the comparison baseline would teach the detector
that the broken state is normal, and recovery would appear to happen instantly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ._contracts import RecoveryStatus
from .cluster import all_workloads_ready
from .config import RecoveryConfig
from .phases import ClusterReadout


@dataclass(frozen=True)
class FrozenBaseline:
    """Immutable pre-fault reference. Never updated during a run.

    Adaptive baseline tracking is a separate concern with its own state; it must
    not write here, which is why this is frozen.
    """

    success_rate: float | None
    p95_ms: float | None
    sample_count: int

    @property
    def is_usable(self) -> bool:
        return self.success_rate is not None and self.sample_count > 0


@dataclass
class RecoveryWindow:
    """One evaluation window during recovery observation."""

    index: int
    elapsed_seconds: float
    success_rate: float | None
    p95_ms: float | None
    critical_successes: int
    infra_ready: bool
    healthy: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class RecoveryOutcome:
    status: RecoveryStatus
    recovery_seconds: float | None
    windows: list[RecoveryWindow]
    reason: str

    @property
    def verified(self) -> bool:
        return self.status is RecoveryStatus.VERIFIED


class RecoveryDetector:
    """Evaluates consecutive windows against a frozen baseline."""

    def __init__(
        self,
        baseline: FrozenBaseline,
        config: RecoveryConfig | None = None,
    ) -> None:
        self._baseline = baseline
        self._config = config or RecoveryConfig()
        self._windows: list[RecoveryWindow] = []
        self._consecutive = 0
        self._first_healthy_elapsed: float | None = None
        self._resolved: RecoveryOutcome | None = None

    @property
    def resolved(self) -> RecoveryOutcome | None:
        return self._resolved

    def _evaluate(
        self,
        *,
        success_rate: float | None,
        p95_ms: float | None,
        critical_successes: int,
        readout: ClusterReadout,
    ) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        cfg = self._config

        infra_ready = all_workloads_ready(readout.ready_by_workload)
        if not infra_ready:
            reasons.append("not all workloads report ready == desired")

        if critical_successes < cfg.min_successful_critical_per_window:
            reasons.append(
                f"only {critical_successes} successful critical transactions in this "
                f"window, need {cfg.min_successful_critical_per_window}"
            )

        if success_rate is None:
            reasons.append("success rate unavailable in this window")
        elif self._baseline.success_rate is not None:
            floor = self._baseline.success_rate - cfg.success_rate_tolerance
            if success_rate < floor:
                reasons.append(
                    f"success rate {success_rate:.3f} below baseline floor {floor:.3f}"
                )

        if self._baseline.p95_ms is not None:
            if p95_ms is None:
                reasons.append(
                    "p95 unavailable in this window; latency recovery unconfirmed"
                )
            else:
                ceiling = self._baseline.p95_ms * cfg.p95_regression_factor
                if p95_ms > ceiling:
                    reasons.append(
                        f"p95 {p95_ms:.0f} ms above ceiling {ceiling:.0f} ms "
                        f"({cfg.p95_regression_factor}x baseline)"
                    )

        return (not reasons), reasons

    def observe_window(
        self,
        *,
        elapsed_seconds: float,
        success_rate: float | None,
        p95_ms: float | None,
        critical_successes: int,
        readout: ClusterReadout,
    ) -> RecoveryOutcome | None:
        """Feed one window. Returns an outcome once resolved, else ``None``."""
        if self._resolved is not None:
            return self._resolved

        healthy, reasons = self._evaluate(
            success_rate=success_rate,
            p95_ms=p95_ms,
            critical_successes=critical_successes,
            readout=readout,
        )

        window = RecoveryWindow(
            index=len(self._windows),
            elapsed_seconds=elapsed_seconds,
            success_rate=success_rate,
            p95_ms=p95_ms,
            critical_successes=critical_successes,
            infra_ready=all_workloads_ready(readout.ready_by_workload),
            healthy=healthy,
            reasons=reasons,
        )
        self._windows.append(window)

        if healthy:
            if self._consecutive == 0:
                # Recovery is dated from the first healthy window, not the one
                # that completed the streak -- that is when the system actually
                # came back.
                self._first_healthy_elapsed = elapsed_seconds
            self._consecutive += 1
        else:
            self._consecutive = 0
            self._first_healthy_elapsed = None

        if self._consecutive >= self._config.consecutive_healthy_windows:
            self._resolved = RecoveryOutcome(
                status=RecoveryStatus.VERIFIED,
                recovery_seconds=round(self._first_healthy_elapsed or elapsed_seconds, 3),
                windows=list(self._windows),
                reason=(
                    f"{self._consecutive} consecutive healthy windows with successful "
                    "critical transactions and all workloads ready"
                ),
            )
            return self._resolved

        if elapsed_seconds >= self._config.timeout_seconds:
            self._resolved = RecoveryOutcome(
                status=RecoveryStatus.FAILED,
                recovery_seconds=None,
                windows=list(self._windows),
                reason=(
                    f"observed for the full {self._config.timeout_seconds:.0f}s without "
                    f"{self._config.consecutive_healthy_windows} consecutive healthy "
                    "windows"
                ),
            )
            return self._resolved

        return None

    def abandon(self, reason: str) -> RecoveryOutcome:
        """Stop watching without a verdict.

        Used when the probe died, telemetry was lost, or the operator stopped the
        run. The result is ``unknown`` and never ``failed`` -- we did not observe
        a failure to recover, we stopped observing.
        """
        if self._resolved is not None:
            return self._resolved
        self._resolved = RecoveryOutcome(
            status=RecoveryStatus.UNKNOWN,
            recovery_seconds=None,
            windows=list(self._windows),
            reason=f"recovery observation ended without a verdict: {reason}",
        )
        return self._resolved

    def pending(self) -> RecoveryOutcome:
        """Current non-terminal state, for live display."""
        if self._resolved is not None:
            return self._resolved
        return RecoveryOutcome(
            status=RecoveryStatus.PENDING,
            recovery_seconds=None,
            windows=list(self._windows),
            reason=(
                f"{self._consecutive}/{self._config.consecutive_healthy_windows} "
                "consecutive healthy windows so far"
            ),
        )
