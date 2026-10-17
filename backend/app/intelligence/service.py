"""IntelligencePort implementation: composes guard, policy, learner, planner and diagnosis.

Implements ``app.api.ports.IntelligencePort`` (async). The service never
executes SQL and never mutates the cluster. ALLOW means permission to seek
execution approval from the runtime. Raw SQL and parameters exist only for the
duration of ``analyze_query``/``revalidate`` and are never logged, stored in
decisions or passed to the learner or a model.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable

from app.contracts import (
    AnalysisResult,
    Catalog,
    ExperimentProposal,
    LearningObservation,
    LearningStatus,
    PlanEstimate,
    QueryDecision,
    RunSummary,
    SystemStateSnapshot,
    TopologySnapshot,
)

from .diagnosis import DiagnosisConfig, analyze, to_contract
from .features import build_features
from .learner import StreamingLearner
from .planner import PlannerConfig, plan
from .policy import POLICY_VERSION, PolicyOutcome, QueryPolicyConfig, blocked_by_rule, evaluate
from .query_advice import recommend, rejection_recommendations
from .reasoning import DeterministicProvider, ReasoningBudget, ReasoningProvider, enrich
from .sql_ast import SqlAllowlist, SqlRejected, ValidatedQuery, validate_sql

log = logging.getLogger("app.intelligence")
_RUN_TARGET_LIMIT = 256


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _estimate(estimator: Any, validated: ValidatedQuery, target_id: str) -> PlanEstimate | None:
    """Call the estimator (async or sync). Any failure or foreign shape -> None (EST-001)."""

    try:
        result = estimator.estimate(validated.sql, list(validated.parameters), target_id)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, PlanEstimate):
            return result
        dump = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        return PlanEstimate.model_validate(dump)
    except Exception:
        return None


class IntelligenceService:
    def __init__(
        self,
        allowlist: SqlAllowlist,
        *,
        learner: StreamingLearner | None = None,
        policy_config: QueryPolicyConfig | None = None,
        planner_config: PlannerConfig | None = None,
        diagnosis_config: DiagnosisConfig | None = None,
        provider: ReasoningProvider | None = None,
        provider_limitations: list[str] | None = None,
        budget: ReasoningBudget | None = None,
        now: Callable[[], datetime] = _utc_now,
    ):
        self.allowlist = allowlist
        self.learner = learner or StreamingLearner()
        self.policy_config = policy_config or QueryPolicyConfig()
        self.planner_config = planner_config or PlannerConfig()
        self.diagnosis_config = diagnosis_config or DiagnosisConfig()
        self.provider = provider or DeterministicProvider()
        self.provider_limitations = list(provider_limitations or [])
        self.budget = budget or ReasoningBudget()
        self._now = now
        self._run_targets: OrderedDict[str, str] = OrderedDict()

    # -- SQL ------------------------------------------------------------------

    async def _decide(
        self, request: Any, state: SystemStateSnapshot | None, estimator: Any, request_id: str, *, learn: bool
    ) -> QueryDecision:
        now = self._now()
        sql = getattr(request, "sql", None)
        params = getattr(request, "parameters", None)
        target_id = getattr(request, "target_id", None) or "inventory-lab"
        validated: ValidatedQuery | None = None
        try:
            validated = validate_sql(sql, params, self.allowlist)
        except SqlRejected as rejection:
            outcome: PolicyOutcome = blocked_by_rule(rejection.rule_id)

        estimate: PlanEstimate | None = None
        prediction = None
        recommendations: list[str]
        if validated is not None:
            if estimator is not None:
                estimate = await _estimate(estimator, validated, target_id)
                if estimate is None:
                    log.warning("plan estimate unavailable request_id=%s", request_id)
                elif estimate.request_id != request_id:
                    estimate = estimate.model_copy(update={"request_id": request_id})
            features = build_features(validated.structure, estimate, state, now)
            prediction = self.learner.predict(features)  # before any outcome exists
            outcome = evaluate(
                validated.structure, estimate, state, self.learner.trusted_prediction_ms(prediction), now, self.policy_config
            )
            recs = recommend(validated.structure, outcome.rule_ids, self.allowlist, self.policy_config)
            recommendations = [r.render() for r in recs]
            if learn and outcome.decision != "BLOCK" and state is not None:
                self.learner.register_arrival(request_id, features, state.source, state.active_fault_id is not None)
        else:
            recommendations = [r.render() for r in rejection_recommendations(outcome.rule_ids[0])]
        if learn:
            self.learner.record_decision(outcome.decision)

        reasons = list(outcome.reasons) + ([outcome.risk.render()] if outcome.risk else [])
        decision = QueryDecision(
            source=state.source if state is not None else "live",
            decision_id=_new_id("dec"),
            request_id=request_id,
            decision=outcome.decision,
            rule_ids=list(outcome.rule_ids),
            policy_version=POLICY_VERSION,
            state_id=state.state_id if state is not None else "unavailable",
            plan_estimate=estimate,
            prediction=prediction,
            reasons=reasons,
            recommendations=recommendations,
        )
        log.info("query decision request_id=%s decision=%s rules=%s", request_id, decision.decision, ",".join(decision.rule_ids))
        del validated  # BLOCK leaves nothing to stage; drop the transient candidate
        return decision

    async def analyze_query(self, transient_request: Any, state: SystemStateSnapshot | None, estimator: Any) -> QueryDecision:
        return await self._decide(transient_request, state, estimator, _new_id("req"), learn=True)

    async def revalidate(
        self, transient_request: Any, state: SystemStateSnapshot | None, estimator: Any, request_id: str
    ) -> QueryDecision:
        """Deterministic pre-execution recheck for the runtime. No model, no learning side effects."""

        return await self._decide(transient_request, state, estimator, request_id, learn=False)

    # -- experiments ------------------------------------------------------------

    async def plan(
        self, topology: TopologySnapshot, state: SystemStateSnapshot | None, catalog: Catalog, coverage: dict | None = None
    ) -> list[ExperimentProposal]:
        cov = self.learner.coverage() if not coverage else coverage
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

    # -- learning ---------------------------------------------------------------

    async def observe(self, observation: LearningObservation) -> LearningStatus:
        return self.learner.observe(observation)

    async def get_learning_status(self) -> LearningStatus:
        return self.learner.status()
