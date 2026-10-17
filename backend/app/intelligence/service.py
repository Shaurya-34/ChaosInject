"""IntelligencePort implementation: composes planner and diagnosis.

Implements ``app.api.ports.IntelligencePort`` (async). The service never
mutates the cluster.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Callable

from app.contracts import (
    AnalysisResult,
    Catalog,
    ExperimentProposal,
    RunSummary,
    SystemStateSnapshot,
    TopologySnapshot,
)

from .diagnosis import DiagnosisConfig, analyze, to_contract
from .planner import PlannerConfig, plan
from .reasoning import DeterministicProvider, ReasoningBudget, ReasoningProvider, enrich

_RUN_TARGET_LIMIT = 256


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class IntelligenceService:
    def __init__(
        self,
        *,
        planner_config: PlannerConfig | None = None,
        diagnosis_config: DiagnosisConfig | None = None,
        provider: ReasoningProvider | None = None,
        provider_limitations: list[str] | None = None,
        budget: ReasoningBudget | None = None,
        now: Callable[[], datetime] = _utc_now,
    ):
        self.planner_config = planner_config or PlannerConfig()
        self.diagnosis_config = diagnosis_config or DiagnosisConfig()
        self.provider = provider or DeterministicProvider()
        self.provider_limitations = list(provider_limitations or [])
        self.budget = budget or ReasoningBudget()
        self._now = now
        self._run_targets: OrderedDict[str, str] = OrderedDict()

    # -- experiments ------------------------------------------------------------

    async def plan(
        self, topology: TopologySnapshot, state: SystemStateSnapshot | None, catalog: Catalog, coverage: dict | None = None
    ) -> list[ExperimentProposal]:
        cov = coverage or {}
        return plan(topology, state, catalog, cov, self._now(), self.planner_config)

    def note_run_target(self, run_id: str, target_id: str) -> None:
        """Record which target a run injected (``RunSummary`` does not carry it). Bounded."""

        self._run_targets[run_id] = target_id
        self._run_targets.move_to_end(run_id)
        while len(self._run_targets) > _RUN_TARGET_LIMIT:
            self._run_targets.popitem(last=False)

    async def analyze_run(self, summary: RunSummary, topology: TopologySnapshot) -> AnalysisResult:
        diag, _ = analyze(summary, topology, self.diagnosis_config, self._run_targets.get(summary.run_id))
        if summary.source == "fixture":
            diag = diag.with_(limitations=diag.limitations + ("Run summary is a labelled fixture, not a live run.",))
        if self.provider.mode == "deterministic":
            diag = enrich(diag, summary, self.provider, self.budget, self.provider_limitations)
        else:  # model calls are bounded but blocking; keep them off the event loop
            diag = await asyncio.to_thread(enrich, diag, summary, self.provider, self.budget, self.provider_limitations)
        return to_contract(diag, summary)

