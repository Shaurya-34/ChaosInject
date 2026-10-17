"""Fake adapters implementing the same types as the live ones.

Three audiences:

1. **Other workstreams.** Sachin's API and Gautam's intelligence stage can be
   built against ``FakeTelemetryPort`` before a cluster exists.
2. **Our own tests.** The whole pipeline runs with no network and no database.
3. **The stub-server slice.** ``ScriptedTransport`` lets the probe and phase
   machinery be exercised end to end before Ananya's deployment is up.

One rule, enforced by ``Source.FIXTURE`` on everything produced here: a fake
result is never presented as a live run. The source field travels all the way
into the report, so a fixture cannot be mistaken for a measurement.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import timedelta

from ._contracts import (
    LoadProfile,
    OperationAggregate,
    Phase,
    PhaseSummary,
    ReadyState,
    RunSummary,
    Source,
    SystemStateSnapshot,
    new_id,
)
from .clock import now_utc
from .cluster import ClusterReadout, StaticClusterAdapter
from .phases import PhaseHandle
from .sampling import LatencyHistogram
from .summary import RuntimeFacts
from .transport import (
    ProbeResponse,
    TransportConnectionError,
    TransportTimeout,
)

__all__ = [
    "FakeTelemetryPort",
    "ScriptedTransport",
    "ScriptedStep",
    "healthy_readout",
    "degraded_readout",
    "make_load_profile",
    "make_operation_aggregate",
    "make_phase_summary",
]


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


@dataclass
class ScriptedStep:
    """One scripted HTTP outcome."""

    status_code: int = 200
    body: str = "OK"
    elapsed_ms: float = 50.0
    raise_timeout: bool = False
    raise_connection_error: bool = False


class ScriptedTransport:
    """Deterministic transport driven by a cycling script.

    Used to produce a known mix of successes, semantic failures and timeouts so
    aggregation can be asserted exactly rather than approximately.
    """

    def __init__(self, steps: list[ScriptedStep]) -> None:
        if not steps:
            raise ValueError("ScriptedTransport needs at least one step")
        self._cycle = itertools.cycle(steps)
        self.closed = False
        self.requests = 0

    async def _next(self) -> ProbeResponse:
        self.requests += 1
        step = next(self._cycle)
        if step.raise_timeout:
            raise TransportTimeout()
        if step.raise_connection_error:
            raise TransportConnectionError()
        return ProbeResponse(
            status_code=step.status_code,
            body_head=step.body,
            elapsed_ms=step.elapsed_ms,
        )

    async def get(self, path: str) -> ProbeResponse:
        return await self._next()

    async def post(self, path: str, form: dict[str, str]) -> ProbeResponse:
        return await self._next()

    async def aclose(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------
# cluster readouts
# --------------------------------------------------------------------------


def healthy_readout(workloads: dict[str, int] | None = None) -> ClusterReadout:
    """All workloads at desired replicas; resource metrics honestly absent."""
    workloads = workloads or {
        "frontend": 1,
        "cartservice": 1,
        "redis-cart": 1,
        "checkoutservice": 1,
    }
    return ClusterReadout(
        ready_by_workload={
            name: ReadyState(ready=count, desired=count)
            for name, count in workloads.items()
        },
        cpu_utilization=None,
        memory_utilization=None,
        missing_features=[
            "cpu_utilization (no metrics-server on kind)",
            "memory_utilization (no metrics-server on kind)",
        ],
    )


def degraded_readout(down: str = "redis-cart") -> ClusterReadout:
    """One workload with zero ready replicas."""
    readout = healthy_readout()
    ready = dict(readout.ready_by_workload)
    if down in ready:
        ready[down] = ReadyState(ready=0, desired=ready[down].desired)
    return ClusterReadout(
        ready_by_workload=ready,
        cpu_utilization=None,
        memory_utilization=None,
        missing_features=list(readout.missing_features),
    )


# --------------------------------------------------------------------------
# object builders
# --------------------------------------------------------------------------


def make_load_profile(
    *,
    load_profile_id: str = "boutique-mixed-v1",
    rate: float = 10.0,
    concurrency: int = 8,
) -> LoadProfile:
    return LoadProfile(
        load_profile_id=load_profile_id,
        generator_version="telemetry-probe-1.0.0",
        operation_mix={
            "browse": 0.40,
            "product_view": 0.25,
            "add_to_cart": 0.20,
            "cart_read": 0.10,
            "checkout": 0.05,
        },
        concurrency=concurrency,
        requested_rate_rps=rate,
        request_timeout_seconds=5.0,
        warmup_seconds=20.0,
        endpoint_identity="fixture://frontend",
    )


def make_operation_aggregate(
    *,
    operation: str = "__all__",
    attempted: int = 600,
    succeeded: int = 598,
    timed_out: int = 0,
    p50_ms: float | None = 80.0,
    p95_ms: float | None = 180.0,
    sample_count: int | None = None,
    missing: list[str] | None = None,
) -> OperationAggregate:
    failed = attempted - succeeded - timed_out
    return OperationAggregate(
        operation=operation,
        attempted=attempted,
        succeeded=succeeded,
        failed=failed,
        timed_out=timed_out,
        error_categories={"http_error": failed} if failed else {},
        success_rate=(succeeded / attempted) if attempted else None,
        p50_ms=p50_ms,
        p95_ms=p95_ms,
        percentile_method=LatencyHistogram().method,
        latency_population="success_only",
        sample_count=sample_count if sample_count is not None else succeeded,
        missing=missing or [],
    )


def make_phase_summary(
    phase: Phase,
    *,
    load_profile: LoadProfile | None = None,
    overall: OperationAggregate | None = None,
    duration_seconds: float = 60.0,
    ready_min: dict[str, ReadyState] | None = None,
) -> PhaseSummary:
    profile = load_profile or make_load_profile()
    agg = overall or make_operation_aggregate()
    started = now_utc()
    return PhaseSummary(
        phase=phase,
        source=Source.FIXTURE,
        started_at=started,
        ended_at=started + timedelta(seconds=duration_seconds),
        duration_seconds=duration_seconds,
        load_profile=profile,
        operations={agg.operation: agg},
        overall=agg,
        achieved_rate_rps=profile.requested_rate_rps,
        offered_rate_rps=profile.requested_rate_rps,
        ready_min=ready_min or {},
        ready_max={},
        percentile_method=agg.percentile_method,
        quality_notes=["synthetic fixture; not a measured run"],
        clock_uncertainty="fixture; no cross-process clock involved",
    )


# --------------------------------------------------------------------------
# port
# --------------------------------------------------------------------------


@dataclass
class FakeTelemetryPort:
    """Fixture-backed ``TelemetryPort`` for other workstreams.

    Everything it returns is marked ``Source.FIXTURE``.
    """

    sandbox_id: str = "bosch-navya"
    state: SystemStateSnapshot | None = None
    summaries: dict[str, RunSummary] = field(default_factory=dict)
    cluster: StaticClusterAdapter = field(
        default_factory=lambda: StaticClusterAdapter(healthy_readout())
    )
    _open: dict[str, PhaseHandle] = field(default_factory=dict, repr=False)

    async def get_state(self, sandbox_id: str) -> SystemStateSnapshot:
        if self.state is not None:
            return self.state
        readout = await self.cluster.read()
        captured = now_utc()
        return SystemStateSnapshot(
            state_id=new_id("state"),
            source=Source.FIXTURE,
            captured_at=captured,
            valid_until=captured + timedelta(seconds=5),
            sandbox_id=sandbox_id,
            ready_by_workload=readout.ready_by_workload,
            request_rate=10.0,
            error_rate=0.002,
            p95_ms=180.0,
            missing_features=list(readout.missing_features),
            sample_count=300,
        )

    def begin_phase(
        self, run_id: str, phase: Phase, load_profile: LoadProfile
    ) -> PhaseHandle:
        from .phases import MonoAnchor

        handle = PhaseHandle(
            run_id=run_id,
            phase=phase,
            load_profile=load_profile,
            anchor=MonoAnchor.capture(),
            started_mono=0.0,
            started_at=now_utc(),
        )
        self._open[f"{run_id}:{phase.value}"] = handle
        return handle

    def close_phase(self, phase_handle: PhaseHandle) -> PhaseSummary:
        phase_handle._closed = True
        self._open.pop(f"{phase_handle.run_id}:{phase_handle.phase.value}", None)
        return make_phase_summary(
            phase_handle.phase, load_profile=phase_handle.load_profile
        )

    def finalize_run(self, run_id: str, runtime_facts: RuntimeFacts) -> RunSummary:
        if run_id in self.summaries:
            return self.summaries[run_id]
        raise ValueError(
            f"FakeTelemetryPort has no fixture summary for run {run_id!r}; "
            "load one from fixtures/v1/telemetry/"
        )
