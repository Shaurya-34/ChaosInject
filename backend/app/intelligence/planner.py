"""Experiment planner: deterministic admissibility filter, then a published rubric.

Score = 0.35*impact + 0.25*criticality + 0.15*uncertainty + 0.15*coverage_gap
        + 0.05*reversibility - 0.05*execution_cost

  impact        transitive callers of the target (reverse traversal of
                caller -> dependency edges) / (nodes - 1)
  criticality   operator-reviewed business criticality of the target, 0..1
  uncertainty   1 - lowest confidence among edges into the target
  coverage_gap  1 / (1 + times this catalog/target pair has been run)
  reversibility 1 if the fault is undone by cleanup, else 0
  execution_cost relative cost from the catalog rule table, 0..1

Candidates are excluded before scoring. Proposals carry bounded parameters
only: no selectors, manifests or commands. Tooshita's runtime validation stays
authoritative; a proposal is a suggestion to validate, never permission.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping

from app.contracts import (
    AbortLimits,
    Catalog,
    CatalogEntry,
    ExperimentProposal,
    FaultParameters,
    SystemStateSnapshot,
    TopologyNode,
    TopologySnapshot,
)

from .dependency_map import operations_for

PLANNER_VERSION = "planner-rubric-1"

DEFAULT_WEIGHTS: Mapping[str, float] = {
    "impact": 0.35,
    "criticality": 0.25,
    "uncertainty": 0.15,
    "coverage_gap": 0.15,
    "reversibility": 0.05,
    "execution_cost": -0.05,
}

# Operator-reviewed rubric input for Online Boutique, keyed by workload name.
# These are judgements, not measurements; edit in review, not at runtime.
DEFAULT_CRITICALITY: Mapping[str, float] = {
    "frontend": 1.0,
    "checkoutservice": 1.0,
    "cartservice": 1.0,
    "redis-cart": 1.0,
    "paymentservice": 1.0,
    "productcatalogservice": 0.9,
    "shippingservice": 0.8,
    "currencyservice": 0.7,
    "emailservice": 0.4,
    "recommendationservice": 0.3,
    "adservice": 0.2,
}


@dataclass(frozen=True)
class CatalogRule:
    """How a catalog fault may be applied. Shape rules only; runtime validates identity."""

    deployment_only: bool
    requires_stateless: bool
    route_only: bool  # only targets behind a verified proxy route
    reversible: bool
    execution_cost: float
    latency_ms: int | None = None


DEFAULT_RULES: Mapping[str, CatalogRule] = {
    "EXP-00": CatalogRule(deployment_only=True, requires_stateless=True, route_only=False, reversible=True, execution_cost=0.3),
    "EXP-01": CatalogRule(deployment_only=True, requires_stateless=True, route_only=False, reversible=False, execution_cost=0.2),
    "EXP-02": CatalogRule(deployment_only=False, requires_stateless=False, route_only=True, reversible=True, execution_cost=0.4),
    "EXP-03": CatalogRule(
        deployment_only=False, requires_stateless=False, route_only=True, reversible=True, execution_cost=0.4, latency_ms=200
    ),
}


@dataclass(frozen=True)
class PlannerConfig:
    weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    criticality: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_CRITICALITY))
    rules: Mapping[str, CatalogRule] = field(default_factory=lambda: dict(DEFAULT_RULES))
    # Targets with a verified Toxiproxy route (Ananya's cart -> Redis overlay).
    route_targets: frozenset[str] = frozenset({"redis-cart"})
    default_criticality: float = 0.5
    default_duration_seconds: int = 30
    abort_limits: AbortLimits = field(
        default_factory=lambda: AbortLimits(
            error_rate=0.5, p95_ms=3000.0, metric_max_age_seconds=10, recovery_timeout_seconds=120, minimum_samples=20
        )
    )
    load_profile_id: str = "steady-browse-cart-checkout-v1"
    chaos_policy_version: str = "chaos-policy-1"
    proposal_ttl_seconds: int = 900
    max_proposals: int = 5
    baseline_max_error_rate: float = 0.02


@dataclass(frozen=True)
class CandidateScore:
    catalog_id: str
    target_id: str
    score: float | None
    components: Mapping[str, float]
    blast_radius: tuple[str, ...]
    excluded_reason: str | None


def callers_of(topology: TopologySnapshot, target_id: str) -> tuple[str, ...]:
    """Every workload that transitively calls ``target_id``."""

    reverse: dict[str, set[str]] = {}
    for edge in topology.edges:
        reverse.setdefault(edge.dependency, set()).add(edge.caller)
    seen: set[str] = set()
    queue = deque([target_id])
    while queue:
        for caller in reverse.get(queue.popleft(), ()):
            if caller not in seen and caller != target_id:
                seen.add(caller)
                queue.append(caller)
    return tuple(sorted(seen))


def _shape_ok(rule: CatalogRule, node: TopologyNode, config: PlannerConfig) -> str | None:
    if rule.route_only and node.target_id not in config.route_targets:
        return "NO_VERIFIED_ROUTE"
    if rule.deployment_only and node.kind != "Deployment":
        return "NOT_A_DEPLOYMENT"
    if rule.requires_stateless and node.stateful:
        return "STATEFUL_TARGET"
    return None


def _exclusion(
    entry: CatalogEntry,
    node: TopologyNode,
    topology: TopologySnapshot,
    state: SystemStateSnapshot | None,
    now: datetime,
    config: PlannerConfig,
) -> str | None:
    if state is None or now > state.valid_until:
        return "STATE_STALE"
    if state.sandbox_id != topology.sandbox_id:
        return "SANDBOX_MISMATCH"
    if state.active_fault_id is not None:
        return "FAULT_ACTIVE"
    if state.error_rate is None:
        return "BASELINE_UNKNOWN"
    if state.error_rate > config.baseline_max_error_rate:
        return "BASELINE_UNHEALTHY"
    if not entry.available:
        return "CATALOG_UNAVAILABLE"
    if entry.catalog_id not in config.rules:
        return "NO_CATALOG_RULE"
    ready = state.ready_by_workload.get(node.name) or state.ready_by_workload.get(node.target_id)
    if ready is None or ready.ready is None or ready.desired is None:
        return "READINESS_UNKNOWN"
    if ready.desired == 0 or ready.ready < ready.desired:
        return "TARGET_NOT_READY"
    return None


def rank_candidates(
    topology: TopologySnapshot,
    state: SystemStateSnapshot | None,
    catalog: Catalog,
    coverage: Mapping[tuple[str, str], int],
    now: datetime,
    config: PlannerConfig,
) -> list[CandidateScore]:
    nodes = {n.target_id: n for n in topology.nodes}
    n_others = max(1, len(nodes) - 1)
    out: list[CandidateScore] = []
    for entry in sorted(catalog.entries, key=lambda e: e.catalog_id):
        rule = config.rules.get(entry.catalog_id)
        for target_id in sorted(nodes):
            node = nodes[target_id]
            callers = callers_of(topology, target_id)
            reason = _shape_ok(rule, node, config) if rule else "NO_CATALOG_RULE"
            if reason == "NO_VERIFIED_ROUTE":
                continue  # not a candidate at all for route faults
            reason = reason or _exclusion(entry, node, topology, state, now, config)
            if reason is not None:
                out.append(CandidateScore(entry.catalog_id, target_id, None, {}, callers, reason))
                continue
            in_conf = [e.confidence for e in topology.edges if e.dependency == target_id]
            components = {
                "impact": len(callers) / n_others,
                "criticality": float(config.criticality.get(node.name, config.default_criticality)),
                "uncertainty": 1.0 - (min(in_conf) if in_conf else node.confidence),
                "coverage_gap": 1.0 / (1.0 + coverage.get((entry.catalog_id, target_id), 0)),
                "reversibility": 1.0 if rule.reversible else 0.0,
                "execution_cost": rule.execution_cost,
            }
            score = sum(config.weights[k] * v for k, v in components.items())
            out.append(
                CandidateScore(
                    entry.catalog_id,
                    target_id,
                    round(score, 4),
                    {k: round(v, 4) for k, v in components.items()},
                    callers,
                    None,
                )
            )
    out.sort(key=lambda c: (c.score is None, -(c.score or 0.0), c.catalog_id, c.target_id))
    return out


def _hypothesis(entry: CatalogEntry, node: TopologyNode, params: FaultParameters) -> str:
    hard, soft = operations_for(node.name)
    hard_txt = ", ".join(hard) if hard else "no mapped workload operation"
    soft_txt = f" Operations that only soft-depend on it ({', '.join(soft)}) should keep succeeding." if soft else ""
    if entry.catalog_id == "EXP-00":
        lead = f"While {node.name} is scaled to zero for {params.duration_seconds}s"
    elif entry.catalog_id == "EXP-01":
        lead = f"After one Pod of {node.name} ({node.desired_replicas} desired replica(s)) is deleted"
    elif entry.catalog_id == "EXP-02":
        lead = f"While the verified route to {node.name} is interrupted for {params.duration_seconds}s"
    else:
        lead = f"With {params.latency_ms} ms added on the verified route to {node.name}"
    return (
        f"Predicted: {lead}, operations that hard-depend on it ({hard_txt}) degrade.{soft_txt} "
        f"{node.name} returns to its desired ready state and transactions succeed within the recovery timeout."
    )


def plan(
    topology: TopologySnapshot,
    state: SystemStateSnapshot | None,
    catalog: Catalog,
    coverage: Mapping[tuple[str, str], int],
    now: datetime,
    config: PlannerConfig,
) -> list[ExperimentProposal]:
    if state is None:
        return []
    nodes = {n.target_id: n for n in topology.nodes}
    entries = {e.catalog_id: e for e in catalog.entries}
    proposals: list[ExperimentProposal] = []
    for cand in rank_candidates(topology, state, catalog, coverage, now, config):
        if cand.score is None or len(proposals) >= config.max_proposals:
            continue
        entry, node = entries[cand.catalog_id], nodes[cand.target_id]
        rule = config.rules[entry.catalog_id]
        params = FaultParameters(
            duration_seconds=min(config.default_duration_seconds, entry.max_duration_seconds),
            latency_ms=rule.latency_ms,
        )
        reason = f"{PLANNER_VERSION} score {cand.score:.3f}: " + ", ".join(
            f"{k}={v:.2f}" for k, v in cand.components.items()
        )
        proposals.append(
            ExperimentProposal(
                source=state.source,
                proposal_id=f"prop_{uuid.uuid4().hex}",
                catalog_id=entry.catalog_id,
                sandbox_id=topology.sandbox_id,
                target_id=node.target_id,
                fault_parameters=params,
                hypothesis=_hypothesis(entry, node, params),
                reason=reason,
                expected_blast_radius=list(cand.blast_radius),
                load_profile_id=config.load_profile_id,
                abort_limits=config.abort_limits,
                policy_version=config.chaos_policy_version,
                expires_at=now + timedelta(seconds=config.proposal_ttl_seconds),
            )
        )
    return proposals
