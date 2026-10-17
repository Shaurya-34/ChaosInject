"""Fixed-size numeric feature vector for streaming learning.

Inputs are the sanitized ``QueryStructure``, the numeric ``PlanEstimate`` and the
``SystemStateSnapshot`` captured at decision time. No SQL text, literals,
parameters, identities, query fingerprints or table names enter the vector, so
its dimensionality never grows with new queries. Unknown inputs become 0 with an
explicit ``*_missing`` indicator set to 1.

Arrivals reported by telemetry carry the shared 8-field ``LearningFeatures``;
``from_learning_features`` maps them onto the same vector, leaving the
structure-only dimensions at 0 and flagging the plan dimensions missing.
"""

from __future__ import annotations

import math
from datetime import datetime

from app.contracts import LearningFeatures, PlanEstimate, SystemStateSnapshot

from .sql_ast import QueryStructure

FEATURE_SET_VERSION = "features-1"

# (name, lower clamp, upper clamp). Order is part of the contract.
FEATURE_SPEC: tuple[tuple[str, float, float], ...] = (
    ("log_est_rows", 0.0, 25.0),
    ("log_est_bytes", 0.0, 40.0),
    ("log_planner_cost", 0.0, 30.0),
    ("est_rows_missing", 0.0, 1.0),
    ("est_bytes_missing", 0.0, 1.0),
    ("planner_cost_missing", 0.0, 1.0),
    ("stats_not_good", 0.0, 1.0),
    ("n_tables", 0.0, 8.0),
    ("n_joins", 0.0, 8.0),
    ("n_predicates", 0.0, 16.0),
    ("n_aggregates", 0.0, 8.0),
    ("n_projected_columns", 0.0, 64.0),
    ("select_star", 0.0, 1.0),
    ("has_where", 0.0, 1.0),
    ("has_limit", 0.0, 1.0),
    ("has_order_by", 0.0, 1.0),
    ("has_group_by", 0.0, 1.0),
    ("aggregate_only", 0.0, 1.0),
    ("filters_on_indexed", 0.0, 1.0),
    ("plan_seq_scan", 0.0, 1.0),
    ("plan_index_scan", 0.0, 1.0),
    ("plan_sort", 0.0, 1.0),
    ("plan_join", 0.0, 1.0),
    ("plan_aggregate", 0.0, 1.0),
    ("log_state_p95_ms", 0.0, 15.0),
    ("state_p95_missing", 0.0, 1.0),
    ("state_error_rate", 0.0, 1.0),
    ("state_error_rate_missing", 0.0, 1.0),
    ("db_conn_utilization", 0.0, 1.0),
    ("db_conn_missing", 0.0, 1.0),
    ("ready_fraction", 0.0, 1.0),
    ("ready_fraction_missing", 0.0, 1.0),
    ("fault_active", 0.0, 1.0),
    ("state_stale", 0.0, 1.0),
)
FEATURE_NAMES: tuple[str, ...] = tuple(name for name, _, _ in FEATURE_SPEC)
_BOUNDS = {name: (lo, hi) for name, lo, hi in FEATURE_SPEC}
GOOD_STATS = frozenset({"fresh", "fixture"})


def _log(value: float | None) -> tuple[float, float]:
    if value is None or not math.isfinite(value) or value < 0:
        return 0.0, 1.0
    return math.log1p(value), 0.0


def _known(value: float | None) -> tuple[float, float]:
    return (0.0, 1.0) if value is None else (float(value), 0.0)


def sanitize_features(raw: dict[str, float] | None) -> dict[str, float]:
    """Return a full, clamped vector with exactly ``FEATURE_NAMES`` keys.

    Unknown keys are dropped; missing/non-finite values become 0. This keeps the
    model's dimensionality fixed regardless of what an observation carries.
    """

    raw = raw or {}
    out: dict[str, float] = {}
    for name in FEATURE_NAMES:
        value = raw.get(name, 0.0)
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 0.0
        if not math.isfinite(value):
            value = 0.0
        lo, hi = _BOUNDS[name]
        out[name] = min(hi, max(lo, value))
    return out


def plan_categories(estimate: PlanEstimate | None) -> set[str]:
    return {c.lower() for c in estimate.node_categories} if estimate else set()


def build_features(
    structure: QueryStructure,
    estimate: PlanEstimate | None,
    state: SystemStateSnapshot | None,
    now: datetime,
) -> dict[str, float]:
    f: dict[str, float] = {}
    usable = estimate is not None and estimate.plan_source != "unavailable"
    rows = estimate.estimated_rows if usable else None
    out_bytes = estimate.estimated_output_bytes if usable else None
    cost = estimate.planner_cost if usable else None
    f["log_est_rows"], f["est_rows_missing"] = _log(rows)
    f["log_est_bytes"], f["est_bytes_missing"] = _log(out_bytes)
    f["log_planner_cost"], f["planner_cost_missing"] = _log(cost)
    f["stats_not_good"] = 0.0 if usable and estimate.stats_quality in GOOD_STATS else 1.0

    f["n_tables"] = len(structure.tables)
    f["n_joins"] = structure.n_joins
    f["n_predicates"] = structure.n_predicates
    f["n_aggregates"] = structure.n_aggregates
    f["n_projected_columns"] = structure.n_projected_columns
    for flag in ("select_star", "has_where", "has_limit", "has_order_by", "has_group_by", "aggregate_only", "filters_on_indexed"):
        f[flag] = 1.0 if getattr(structure, flag) else 0.0

    cats = plan_categories(estimate) if usable else set()
    f["plan_seq_scan"] = 1.0 if "seq_scan" in cats else 0.0
    f["plan_index_scan"] = 1.0 if cats & {"index_scan", "index_only_scan", "bitmap_scan"} else 0.0
    f["plan_sort"] = 1.0 if cats & {"sort", "incremental_sort"} else 0.0
    f["plan_join"] = 1.0 if cats & {"hash_join", "merge_join", "nested_loop"} else 0.0
    f["plan_aggregate"] = 1.0 if "aggregate" in cats else 0.0

    f.update(_state_features(state, now))
    return sanitize_features(f)


def _state_features(state: SystemStateSnapshot | None, now: datetime | None) -> dict[str, float]:
    f: dict[str, float] = {}
    if state is None:
        for name in ("state_p95_missing", "state_error_rate_missing", "db_conn_missing", "ready_fraction_missing", "state_stale"):
            f[name] = 1.0
        return f
    f["log_state_p95_ms"], f["state_p95_missing"] = _log(state.p95_ms)
    f["state_error_rate"], f["state_error_rate_missing"] = _known(state.error_rate)
    f["db_conn_utilization"], f["db_conn_missing"] = _known(connection_utilization(state))
    f["ready_fraction"], f["ready_fraction_missing"] = _known(ready_fraction(state))
    f["fault_active"] = 1.0 if state.active_fault_id else 0.0
    f["state_stale"] = 1.0 if now is not None and now > state.valid_until else 0.0
    return f


def from_learning_features(lf: LearningFeatures, active_fault: bool) -> dict[str, float]:
    """Map telemetry's shared ``LearningFeatures`` onto the fixed vector."""

    f: dict[str, float] = {}
    f["log_est_bytes"], f["est_bytes_missing"] = _log(lf.estimated_output_bytes)
    f["est_rows_missing"] = 1.0
    f["planner_cost_missing"] = 1.0
    f["stats_not_good"] = 1.0
    f["plan_join"] = 1.0 if lf.has_join else 0.0
    f["plan_sort"] = 1.0 if lf.has_sort else 0.0
    f["has_order_by"] = 1.0 if lf.has_sort else 0.0
    f["log_state_p95_ms"], f["state_p95_missing"] = _log(lf.p95_ms)
    f["state_error_rate"], f["state_error_rate_missing"] = _known(lf.error_rate)
    f["db_conn_utilization"], f["db_conn_missing"] = _known(lf.connection_utilization)
    f["ready_fraction"], f["ready_fraction_missing"] = _known(lf.ready_fraction)
    f["fault_active"] = 1.0 if active_fault else 0.0
    return sanitize_features(f)


def connection_utilization(state: SystemStateSnapshot) -> float | None:
    if state.db_connections_used is None or not state.db_connections_limit:
        return None
    return min(1.0, state.db_connections_used / state.db_connections_limit)


def ready_fraction(state: SystemStateSnapshot) -> float | None:
    known = [(r.ready, r.desired) for r in state.ready_by_workload.values() if r.ready is not None and r.desired]
    desired = sum(d for _, d in known)
    if desired == 0:
        return None
    return sum(min(r, d) for r, d in known) / desired
