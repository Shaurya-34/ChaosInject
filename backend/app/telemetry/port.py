"""``TelemetryPort`` -- the interface the rest of the backend talks to.

From the shared interfaces file:

    TelemetryPort.get_state(sandbox_id)                  -> SystemStateSnapshot
    TelemetryPort.begin_phase(run_id, phase, profile)    -> phase_handle
    TelemetryPort.close_phase(phase_handle)              -> PhaseSummary
    TelemetryPort.finalize_run(run_id, runtime_facts)    -> RunSummary

``LiveTelemetryPort`` wires the pieces together. ``FakeTelemetryPort`` (in
``fakes.py``) implements the same protocol from fixtures, so Sachin's API and
Gautam's intelligence stage can be built and tested before a cluster exists.

Note what this port does **not** expose: there is no method here that mutates
anything. Telemetry observes. Mutation authority lives entirely in Tooshita's
runtime, and keeping that boundary visible in the type signature is deliberate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ._contracts import (
    LoadProfile,
    Phase,
    PhaseSummary,
    RunSummary,
    Source,
    SystemStateSnapshot,
)
from .clock import ClockUncertainty
from .cluster import ClusterAdapter
from .config import TelemetryConfig
from .phases import MonoAnchor, PhaseHandle, PhaseRecorder
from .recovery import RecoveryOutcome
from .sampling import BoundedRing
from .state import StateBuilder
from .summary import RuntimeFacts, finalize_run


class TelemetryPort(Protocol):
    async def get_state(self, sandbox_id: str) -> SystemStateSnapshot: ...

    def begin_phase(
        self, run_id: str, phase: Phase, load_profile: LoadProfile
    ) -> PhaseHandle: ...

    def close_phase(self, phase_handle: PhaseHandle) -> PhaseSummary: ...

    def finalize_run(self, run_id: str, runtime_facts: RuntimeFacts) -> RunSummary: ...


class LiveTelemetryPort:
    """Composition root for the telemetry workstream."""

    def __init__(
        self,
        sandbox_id: str,
        cluster: ClusterAdapter,
        *,
        config: TelemetryConfig | None = None,
        source: Source = Source.LIVE,
        anchor: MonoAnchor | None = None,
    ) -> None:
        self._config = config or TelemetryConfig()
        self._sandbox_id = sandbox_id
        self._source = source
        self.recorder = PhaseRecorder(
            anchor=anchor, sampling=self._config.sampling, source=source
        )
        self.state_builder = StateBuilder(
            sandbox_id,
            self.recorder.ring,
            cluster,
            state_config=self._config.state,
            sampling_config=self._config.sampling,
            source=source,
        )
        self._cluster = cluster
        self._closed: dict[str, dict[str, PhaseSummary]] = {}
        self._profiles: dict[str, LoadProfile] = {}
        self._recovery: dict[str, RecoveryOutcome] = {}
        self._clock_uncertainty = ClockUncertainty()

    # -- port ---------------------------------------------------------------

    async def get_state(self, sandbox_id: str) -> SystemStateSnapshot:
        if sandbox_id != self._sandbox_id:
            raise ValueError(
                f"telemetry is bound to sandbox {self._sandbox_id!r}; "
                f"refusing a request for {sandbox_id!r}"
            )
        return await self.state_builder.capture()

    def begin_phase(
        self,
        run_id: str,
        phase: Phase,
        load_profile: LoadProfile,
        *,
        at_utc: datetime | None = None,
    ) -> PhaseHandle:
        self._profiles.setdefault(run_id, load_profile)
        return self.recorder.begin_phase(
            run_id,
            phase,
            load_profile,
            at_utc=at_utc,
            clock_uncertainty=self._clock_uncertainty,
        )

    def close_phase(
        self,
        phase_handle: PhaseHandle,
        *,
        at_utc: datetime | None = None,
        achieved_rate_rps: float | None = None,
    ) -> PhaseSummary:
        summary = self.recorder.close_phase(
            phase_handle, at_utc=at_utc, achieved_rate_rps=achieved_rate_rps
        )
        self._closed.setdefault(phase_handle.run_id, {})[
            phase_handle.phase.value
        ] = summary
        return summary

    def finalize_run(self, run_id: str, runtime_facts: RuntimeFacts) -> RunSummary:
        phases = self._closed.get(run_id)
        if not phases:
            raise ValueError(f"no closed phases recorded for run {run_id!r}")
        profile = self._profiles.get(run_id)
        if profile is None:
            raise ValueError(f"no load profile recorded for run {run_id!r}")

        return finalize_run(
            run_id=run_id,
            phase_summaries=phases,
            load_profile=profile,
            facts=runtime_facts,
            recovery=self._recovery.get(run_id),
            sampling=self._config.sampling,
            dropped_samples=self.recorder.ring.dropped_total,
            clock_uncertainty=self._clock_uncertainty,
            source=self._source,
        )

    # -- orchestration helpers ---------------------------------------------

    def set_recovery_outcome(self, run_id: str, outcome: RecoveryOutcome) -> None:
        self._recovery[run_id] = outcome

    def set_clock_uncertainty(self, uncertainty: ClockUncertainty) -> None:
        """Called once the runtime handshake measures cross-process skew."""
        self._clock_uncertainty = uncertainty

    def set_active_fault(self, fault_id: str | None) -> None:
        self.state_builder.set_active_fault(fault_id)

    async def observe_readiness(self) -> None:
        """Poll the cluster and feed readiness extrema into open phases."""
        readout = await self._cluster.read()
        self.recorder.observe_readiness(readout)

    @property
    def ring(self) -> BoundedRing:
        return self.recorder.ring

    @property
    def sink(self):
        """The callable the probe writes samples into."""
        return self.recorder.sink
