"""Versioned deterministic query policy.

Severity is monotone: the final decision is the most severe triggered rule.
Fresh state and trusted learned predictions may only add WARN/BLOCK rules; no
input can remove a rule another input triggered. ``QueryPolicyConfig`` is frozen
and the learner holds no reference to it, so learning cannot relax limits.
Thresholds are demo policy settings to tune, not universal limits.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.contracts import PlanEstimate, SystemStateSnapshot

from .features import GOOD_STATS, connection_utilization, plan_categories
from .internal import QueryRisk
from .sql_ast import QueryStructure

POLICY_VERSION = "sqlguard-2026.10.1"

Decision = Literal["ALLOW", "WARN", "BLOCK"]
_SEVERITY = {"ALLOW": 0, "WARN": 1, "BLOCK": 2}

# Rule catalogue. AST rules (SQL-*) are raised by sql_ast.validate_sql.
RULES: dict[str, tuple[Decision, str]] = {
    "SQL-001": ("BLOCK", "Empty or oversized request."),
    "SQL-002": ("BLOCK", "SQL could not be parsed."),
    "SQL-003": ("BLOCK", "Exactly one statement is required."),
    "SQL-004": ("BLOCK", "Only SELECT statements are permitted (no writes, DDL, COPY, DO or utility statements)."),
    "SQL-005": ("BLOCK", "Common table expressions are not supported (this also excludes writable CTEs)."),
    "SQL-006": ("BLOCK", "SELECT INTO is not permitted."),
    "SQL-007": ("BLOCK", "Row locking clauses are not permitted."),
    "SQL-008": ("BLOCK", "Set operations are not supported."),
    "SQL-009": ("BLOCK", "Relation is not on the allowlist."),
    "SQL-010": ("BLOCK", "Column is not on the allowlist or is ambiguous."),
    "SQL-011": ("BLOCK", "Function is not on the allowlist."),
    "SQL-012": ("BLOCK", "Operator is not on the allowlist."),
    "SQL-013": ("BLOCK", "Cast target type is not on the allowlist."),
    "SQL-014": ("BLOCK", "Construct is outside the supported SELECT subset."),
    "SQL-015": ("BLOCK", "Parameters do not match placeholders or have unsupported types."),
    "EST-001": ("BLOCK", "No usable plan estimate; the guard does not guess an ALLOW."),
    "EST-002": ("WARN", "Planner statistics are stale or unknown; estimates are less reliable."),
    "OUT-001": ("BLOCK", "Estimated response size exceeds the console hard limit."),
    "OUT-002": ("BLOCK", "Estimated row count exceeds the console hard limit."),
    "OUT-003": ("BLOCK", "Unbounded row-producing query exceeds the interactive row budget."),
    "OUT-004": ("WARN", "Estimated output exceeds the interactive budget; results will be truncated."),
    "COST-001": ("WARN", "Planner cost is high (planner units, not milliseconds)."),
    "SORT-001": ("WARN", "LIMIT with an explicit sort still sorts the full input."),
    "STATE-001": ("WARN", "System state is missing or stale; load could not be considered."),
    "STATE-002": ("WARN", "Database connection utilisation is elevated."),
    "STATE-003": ("BLOCK", "Database connection utilisation is critical."),
    "STATE-004": ("BLOCK", "A chaos experiment is active; SQL is paused for attribution. Retry later."),
    "PRED-001": ("WARN", "Learned duration estimate is high (advisory; requires review)."),
}


@dataclass(frozen=True)
class QueryPolicyConfig:
    hard_max_output_bytes: int = 32 * 1024 * 1024
    hard_max_rows: int = 50_000
    interactive_max_rows: int = 1_000
    interactive_max_bytes: int = 1024 * 1024
    expensive_plan_cost: float = 100_000.0
    db_conn_warn: float = 0.80
    db_conn_block: float = 0.95
    stale_state_decision: Decision = "WARN"
    predicted_duration_warn_ms: float = 5_000.0


@dataclass(frozen=True)
class PolicyOutcome:
    decision: Decision
    rule_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    risk: QueryRisk | None


def combine(rule_ids: list[str]) -> Decision:
    worst: Decision = "ALLOW"
    for rid in rule_ids:
        severity = RULES[rid][0]
        if _SEVERITY[severity] > _SEVERITY[worst]:
            worst = severity
    return worst


def blocked_by_rule(rule_id: str) -> PolicyOutcome:
    return PolicyOutcome("BLOCK", (rule_id,), (RULES[rule_id][1],), None)


def evaluate(
    structure: QueryStructure,
    estimate: PlanEstimate | None,
    state: SystemStateSnapshot | None,
    trusted_prediction_ms: float | None,
    now: datetime,
    config: QueryPolicyConfig,
) -> PolicyOutcome:
    rules: list[str] = []
    detail: dict[str, str] = {}

    rows = estimate.estimated_rows if estimate else None
    out_bytes = estimate.estimated_output_bytes if estimate else None
    cost = estimate.planner_cost if estimate else None
    if estimate is None or estimate.plan_source == "unavailable" or rows is None or out_bytes is None:
        rules.append("EST-001")
    else:
        if estimate.stats_quality not in GOOD_STATS:
            rules.append("EST-002")
        if out_bytes > config.hard_max_output_bytes:
            rules.append("OUT-001")
            detail["OUT-001"] = f"estimated {out_bytes:.0f} bytes > limit {config.hard_max_output_bytes}"
        if rows > config.hard_max_rows:
            rules.append("OUT-002")
            detail["OUT-002"] = f"estimated {rows:.0f} rows > limit {config.hard_max_rows}"
        row_producing = not structure.aggregate_only
        over_budget = rows > config.interactive_max_rows or out_bytes > config.interactive_max_bytes
        if row_producing and not structure.has_limit and rows > config.interactive_max_rows:
            rules.append("OUT-003")
            detail["OUT-003"] = (
                f"no LIMIT and estimated {rows:.0f} rows > interactive budget {config.interactive_max_rows}"
            )
        elif over_budget:
            rules.append("OUT-004")
            detail["OUT-004"] = (
                f"estimated {rows:.0f} rows / {out_bytes:.0f} bytes; interactive budget "
                f"{config.interactive_max_rows} rows / {config.interactive_max_bytes} bytes"
            )
        if cost is not None and cost > config.expensive_plan_cost:
            rules.append("COST-001")
            detail["COST-001"] = f"planner cost {cost:.0f} > {config.expensive_plan_cost:.0f} (planner units)"
        cats = plan_categories(estimate)
        if structure.has_limit and structure.has_order_by and "sort" in cats and not structure.order_by_indexed:
            rules.append("SORT-001")

    util = None
    if state is None or now > state.valid_until:
        rules.append("STATE-001")
    else:
        util = connection_utilization(state)
        if util is not None and util >= config.db_conn_block:
            rules.append("STATE-003")
        elif util is not None and util >= config.db_conn_warn:
            rules.append("STATE-002")
        if util is not None:
            detail.setdefault("STATE-002", f"connection utilisation {util:.2f} >= {config.db_conn_warn:.2f}")
            detail.setdefault("STATE-003", f"connection utilisation {util:.2f} >= {config.db_conn_block:.2f}")
        if state.active_fault_id is not None:
            rules.append("STATE-004")

    if trusted_prediction_ms is not None and trusted_prediction_ms > config.predicted_duration_warn_ms:
        rules.append("PRED-001")
        detail["PRED-001"] = (
            f"predicted {trusted_prediction_ms:.0f} ms > {config.predicted_duration_warn_ms:.0f} ms (advisory)"
        )

    decision = combine(rules)
    if "STATE-001" in rules and config.stale_state_decision == "BLOCK":
        decision = "BLOCK"
    reasons = tuple(
        RULES[r][1] + (f" ({detail[r]})" if r in detail else "") for r in rules
    ) or ("All deterministic checks passed; ALLOW means permission to seek execution approval.",)
    return PolicyOutcome(decision, tuple(rules), reasons, _risk(structure, estimate, util, config))


def _risk(
    structure: QueryStructure,
    estimate: PlanEstimate | None,
    util: float | None,
    config: QueryPolicyConfig,
) -> QueryRisk | None:
    if (
        estimate is None
        or estimate.plan_source == "unavailable"
        or estimate.estimated_rows is None
        or estimate.estimated_output_bytes is None
    ):
        return None

    def ratio(value: float | None, limit: float) -> float:
        if value is None:
            return 1.0
        return min(1.0, math.log1p(value) / math.log1p(limit))

    components = {
        "output_size": ratio(estimate.estimated_output_bytes, config.hard_max_output_bytes),
        "row_count": ratio(estimate.estimated_rows, config.hard_max_rows),
        "plan_cost": ratio(estimate.planner_cost, config.expensive_plan_cost * 10),
        "connection_pressure": 0.5 if util is None else util,
        "unbounded": 1.0 if (not structure.has_limit and not structure.aggregate_only) else 0.0,
    }
    weights = {"output_size": 0.30, "row_count": 0.25, "plan_cost": 0.15, "connection_pressure": 0.15, "unbounded": 0.15}
    score = 100.0 * sum(weights[k] * components[k] for k in weights)
    return QueryRisk(score=round(score, 1), components={k: round(v, 3) for k, v in components.items()})
