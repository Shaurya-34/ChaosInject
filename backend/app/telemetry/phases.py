"""Measurement phases and their aggregation.

A run produces one continuous stream of samples. Phases carve that stream into
``baseline``, ``fault`` and ``recovery`` windows aligned to the markers Tooshita
emits when a fault is injected and removed.

Those markers arrive as UTC timestamps from a *different process*, while our
sample times are monotonic and local. ``MonoAnchor`` converts between them using
an anchor pair captured at run start, and ``ClockUncertainty`` records how well
that conversion is trusted. The alternative -- quietly treating a remote UTC
timestamp as if it were our monotonic clock -- produces window edges that look
precise and are not.

``PhaseRecorder`` is the sink the probe writes into. Several phases can be open
at once (recovery overlaps the tail of a fault window during verification), and
each takes the samples that fall inside its own boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ._contracts import (
    LoadProfile,
    OperationAggregate,
    Outcome,
    Phase,
    PhaseSummary,
    ReadyState,
    Source,
)
from .clock import ClockUncertainty, mono, now_utc
from .cluster import ClusterReadout
from .config import SamplingConfig
from .sampling import BoundedRing, LatencyHistogram, OperationCounters, Sample


@dataclass(frozen=True)
class MonoAnchor:
    """Anchor pair mapping our monotonic clock to UTC.

    Captured once at run start. Conversion drifts with clock adjustment over a
    long run, which is why the uncertainty travels with every summary rather
    than being assumed away.
    """

    utc_at_anchor: datetime
    mono_at_anchor: float

    @classmethod
    def capture(cls) -> "MonoAnchor":
        return cls(utc_at_anchor=now_utc(), mono_at_anchor=mono())

    def to_mono(self, timestamp: datetime) -> float:
        delta = (timestamp - self.utc_at_anchor).total_seconds()
        return self.mono_at_anchor + delta

    def to_utc(self, mono_t: float) -> datetime:
        return self.utc_at_anchor + timedelta(seconds=mono_t - self.mono_at_anchor)


@dataclass
class PhaseHandle:
    """An open measurement window."""

    run_id: str
    phase: Phase
    load_profile: LoadProfile
    anchor: MonoAnchor
    started_mono: float
    started_at: datetime
    ended_mono: float | None = None
    ended_at: datetime | None = None
    counters: dict[str, OperationCounters] = field(default_factory=dict)
    ready_min: dict[str, ReadyState] = field(default_factory=dict)
    ready_max: dict[str, ReadyState] = field(default_factory=dict)
    offered_attempts: int = 0
    quality_notes: list[str] = field(default_factory=list)
    clock_uncertainty: ClockUncertainty = field(default_factory=ClockUncertainty)
    _closed: bool = False

    @property
    def closed(self) -> bool:
        return self._closed

    def contains(self, sample_mono: float) -> bool:
        if sample_mono < self.started_mono:
            return False
        if self.ended_mono is not None and sample_mono > self.ended_mono:
            return False
        return True

    def accept(self, sample: Sample) -> None:
        if self._closed or not self.contains(sample.mono_t):
            return
        counters = self.counters.get(sample.operation)
        if counters is None:
            counters = OperationCounters(operation=sample.operation)
            self.counters[sample.operation] = counters
        counters.record(sample)
        self.offered_attempts += 1

    def observe_readiness(self, readout: ClusterReadout) -> None:
        """Track per-workload readiness extrema across the window.

        Extrema, not an average: the lowest ready count during a fault is the
        interesting number, and averaging would smooth away the dip entirely.
        """
        for name, state in readout.ready_by_workload.items():
            low = self.ready_min.get(name)
            if low is None or state.ready < low.ready:
                self.ready_min[name] = state
            high = self.ready_max.get(name)
            if high is None or state.ready > high.ready:
                self.ready_max[name] = state


class PhaseRecorder:
    """Routes samples to open phases and builds immutable summaries."""

    def __init__(
        self,
        *,
        anchor: MonoAnchor | None = None,
        sampling: SamplingConfig | None = None,
        source: Source = Source.LIVE,
    ) -> None:
        self._anchor = anchor or MonoAnchor.capture()
        self._sampling = sampling or SamplingConfig()
        self._source = source
        self._open: dict[str, PhaseHandle] = {}
        self._closed: list[PhaseSummary] = []
        self.ring = BoundedRing(self._sampling)

    @property
    def anchor(self) -> MonoAnchor:
        return self._anchor

    # -- sink -------------------------------------------------------------

    def sink(self, sample: Sample) -> None:
        """Probe sample sink. Feeds the rolling ring and every open phase."""
        self.ring.add(sample)
        self.ring.evict_older_than(now_mono=sample.mono_t)
        for handle in self._open.values():
            handle.accept(sample)

    def observe_readiness(self, readout: ClusterReadout) -> None:
        for handle in self._open.values():
            handle.observe_readiness(readout)

    # -- phase lifecycle ---------------------------------------------------

    def begin_phase(
        self,
        run_id: str,
        phase: Phase,
        load_profile: LoadProfile,
        *,
        at_utc: datetime | None = None,
        clock_uncertainty: ClockUncertainty | None = None,
    ) -> PhaseHandle:
        """Open a window.

        ``at_utc`` lets a phase be anchored to a runtime marker that has already
        happened -- for instance opening the fault window at the exact injection
        timestamp rather than whenever this call ran.
        """
        key = f"{run_id}:{phase.value}"
        if key in self._open:
            raise ValueError(f"phase {phase.value} is already open for run {run_id}")
        started_mono = self._anchor.to_mono(at_utc) if at_utc else mono()
        handle = PhaseHandle(
            run_id=run_id,
            phase=phase,
            load_profile=load_profile,
            anchor=self._anchor,
            started_mono=started_mono,
            started_at=at_utc or self._anchor.to_utc(started_mono),
            clock_uncertainty=clock_uncertainty or ClockUncertainty(),
        )
        if at_utc is not None:
            # Replay samples already buffered in the ring that belong inside the
            # retroactively-opened window.
            for sample in self.ring:
                handle.accept(sample)
        self._open[key] = handle
        return handle

    def close_phase(
        self,
        handle: PhaseHandle,
        *,
        at_utc: datetime | None = None,
        achieved_rate_rps: float | None = None,
    ) -> PhaseSummary:
        """Close a window and freeze its summary."""
        if handle.closed:
            raise ValueError("phase is already closed")
        handle.ended_mono = self._anchor.to_mono(at_utc) if at_utc else mono()
        handle.ended_at = at_utc or self._anchor.to_utc(handle.ended_mono)
        if at_utc is not None:
            # An explicit end marker usually arrives *after* the moment it
            # describes (it comes from another process), so the open phase has
            # already absorbed samples that belong outside the window. Rebuild
            # from the ring so the window ends where the marker says it ends.
            self._rebuild_from_ring(handle)
        handle._closed = True
        self._open.pop(f"{handle.run_id}:{handle.phase.value}", None)

        summary = self._build_summary(handle, achieved_rate_rps=achieved_rate_rps)
        self._closed.append(summary)
        return summary

    def _rebuild_from_ring(self, handle: PhaseHandle) -> None:
        """Recompute a window's counters from the ring using its final bounds.

        Only valid while the ring still covers the window start. If samples have
        been evicted past it, rebuilding would silently *under*-count, so we keep
        the incremental counters and disclose that the end edge may include late
        samples. Either way the limitation is stated rather than hidden.
        """
        oldest = next(iter(self.ring), None)
        covers = self.ring.dropped_total == 0 or (
            oldest is not None and oldest.mono_t <= handle.started_mono
        )
        if not covers:
            handle.quality_notes.append(
                "raw samples for the start of this window were already evicted from "
                "the bounded ring, so samples arriving after the end marker could not "
                "be removed; the end edge may include a few late samples"
            )
            return

        handle.counters = {}
        handle.offered_attempts = 0
        for sample in self.ring:
            handle.accept(sample)

    # -- aggregation -------------------------------------------------------

    def _aggregate(self, counters: OperationCounters) -> OperationAggregate:
        minimum = self._sampling.min_samples_for_percentile
        hist: LatencyHistogram = counters.success_latency
        missing: list[str] = []

        if hist.count >= minimum:
            p50 = hist.quantile(0.50)
            p95 = hist.quantile(0.95)
        else:
            p50 = p95 = None
            missing.append(
                f"p50_ms, p95_ms: {hist.count} successful samples is below the "
                f"configured minimum of {minimum} (team convention)"
            )

        if hist.overflow_count:
            missing.append(
                f"{hist.overflow_count} samples exceeded the histogram ceiling; "
                "upper tail is clipped"
            )
        if counters.timed_out:
            missing.append(
                f"{counters.timed_out} attempts timed out and contribute no latency "
                "sample; the success-only distribution understates true tail latency"
            )

        return OperationAggregate(
            operation=counters.operation,
            attempted=counters.attempted,
            succeeded=counters.succeeded,
            failed=counters.failed,
            timed_out=counters.timed_out,
            error_categories=dict(counters.error_categories),
            success_rate=counters.success_rate,
            p50_ms=p50,
            p95_ms=p95,
            percentile_method=hist.method,
            latency_population="success_only",
            sample_count=hist.count,
            missing=missing,
        )

    def _build_summary(
        self, handle: PhaseHandle, *, achieved_rate_rps: float | None
    ) -> PhaseSummary:
        operations = {
            name: self._aggregate(counters)
            for name, counters in sorted(handle.counters.items())
        }

        overall_counters = OperationCounters(operation="__all__")
        for counters in handle.counters.values():
            renamed = OperationCounters(operation="__all__")
            renamed.attempted = counters.attempted
            renamed.succeeded = counters.succeeded
            renamed.failed = counters.failed
            renamed.timed_out = counters.timed_out
            renamed.error_categories = dict(counters.error_categories)
            renamed.success_latency = counters.success_latency
            renamed.all_attempt_latency = counters.all_attempt_latency
            overall_counters = overall_counters.merge(renamed)

        overall = self._aggregate(overall_counters)
        duration = max(
            0.0, (handle.ended_mono or handle.started_mono) - handle.started_mono
        )

        notes = list(handle.quality_notes)
        if not handle.clock_uncertainty.is_known:
            notes.append(handle.clock_uncertainty.describe())
        if self.ring.dropped_total:
            notes.append(
                f"{self.ring.dropped_total} raw samples were evicted from the bounded "
                "ring during the run; aggregates are unaffected but raw replay is partial"
            )

        return PhaseSummary(
            phase=handle.phase,
            source=self._source,
            started_at=handle.started_at,
            ended_at=handle.ended_at or handle.started_at,
            duration_seconds=round(duration, 3),
            load_profile=handle.load_profile,
            operations=operations,
            overall=overall,
            achieved_rate_rps=(
                round(achieved_rate_rps, 3) if achieved_rate_rps is not None else None
            ),
            offered_rate_rps=handle.load_profile.requested_rate_rps,
            ready_min=dict(handle.ready_min),
            ready_max=dict(handle.ready_max),
            percentile_method=overall.percentile_method,
            quality_notes=notes,
            clock_uncertainty=handle.clock_uncertainty.describe(),
        )

    @property
    def closed_summaries(self) -> list[PhaseSummary]:
        return list(self._closed)


def critical_success_count(
    summary: PhaseSummary, critical_operations: tuple[str, ...]
) -> int:
    """Successful attempts across the business-critical operations only.

    Recovery is judged on these, because a Ready pod that cannot complete a
    checkout has not recovered in any sense the user would recognise.
    """
    return sum(
        summary.operations[name].succeeded
        for name in critical_operations
        if name in summary.operations
    )


def outcome_breakdown(samples: list[Sample]) -> dict[str, int]:
    """Counts by outcome. Used by tests and by the live events view."""
    counts: dict[str, int] = {}
    for sample in samples:
        key = (
            sample.outcome.value
            if isinstance(sample.outcome, Outcome)
            else str(sample.outcome)
        )
        counts[key] = counts.get(key, 0) + 1
    return counts
