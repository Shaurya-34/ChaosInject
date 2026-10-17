"""Safe PostgreSQL plan estimation.

``PlanEstimator.estimate`` answers one narrow question: *roughly how much work
would this already-validated read cost?* It answers it by asking the planner,
never by running the query.

Boundaries, because this is the riskiest surface in the telemetry workstream:

* **Input is trusted, internal and transient.** Gautam's AST validator has
  already confirmed the statement is inside the supported read-only subset
  before this method is called. This class must never be wired to a public
  endpoint that accepts raw SQL.
* **Parameters are bound by the driver, never interpolated.** We prepend
  ``EXPLAIN (FORMAT JSON)`` to an already-validated statement and pass
  parameters separately. String-building a statement from parameters here would
  reintroduce injection behind the validator's back.
* **No ANALYZE, ever.** ``EXPLAIN ANALYZE`` executes the query. The whole point
  is to estimate without executing, so the flag never appears in this file.
* **Planning is still execution of a kind.** The planner evaluates constant
  expressions and may call functions, so a short ``statement_timeout`` and
  Gautam's function allowlist and Ananya's restricted role all still apply. The
  timeout is defence in depth, not decoration.
* **Nothing raw leaves.** Statement text, parameters and the raw plan JSON are
  discarded once the sanitised estimate is built. They are never logged, never
  exported, and never sent to the LLM.

On interpreting the output: ``planner_cost`` is an abstract unit and **is not a
duration**. ``estimated_rows`` is the *output* row count, which for a top-level
aggregate is 1 even when the plan scans millions of rows -- so resource risk
follows ``max_node_estimated_rows``, which walks every node. And a ``LIMIT``
above a ``Sort`` does not make the sort cheap, because the sort still consumes
its whole input; that case is flagged in ``uncertainty``.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, Sequence

from ._contracts import PlanEstimate, Source, new_id
from .clock import now_utc
from .config import PlanEstimatorConfig
from .redaction import redact_exception

logger = logging.getLogger(__name__)

# Nodes that must consume their entire input before producing a first row. A
# LIMIT above one of these does not bound the work the node performs.
BLOCKING_NODE_TYPES = frozenset(
    {"Sort", "Hash", "Aggregate", "GroupAggregate", "HashAggregate", "Materialize"}
)

# Nodes whose estimated row count reflects rows *scanned*, which is the number
# that matters for resource pressure.
SCAN_NODE_TYPES = frozenset(
    {
        "Seq Scan",
        "Index Scan",
        "Index Only Scan",
        "Bitmap Heap Scan",
        "Bitmap Index Scan",
        "Tid Scan",
        "Function Scan",
        "CTE Scan",
    }
)


class PlanUnavailable(Exception):
    """The planner could not be consulted. Carries no statement text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PlanSource(Protocol):
    """Supplies raw ``EXPLAIN (FORMAT JSON)`` output.

    Implementations must not execute the candidate statement and must not retain
    it after returning.
    """

    async def explain_json(
        self, sql: str, parameters: Sequence[Any] | None, *, timeout_ms: int
    ) -> list[dict[str, Any]]: ...

    async def stats_freshness_hours(self) -> float | None: ...


class PsycopgPlanSource:
    """Live plan source over a restricted, read-only role.

    ``psycopg`` is imported lazily so the telemetry package imports cleanly in an
    environment without a database driver.
    """

    def __init__(self, dsn: str, config: PlanEstimatorConfig | None = None) -> None:
        self._config = config or PlanEstimatorConfig()
        self._dsn = dsn

    async def _connect(self):
        try:
            import psycopg  # noqa: PLC0415 - deliberately lazy
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise PlanUnavailable("driver_unavailable") from exc
        return await psycopg.AsyncConnection.connect(
            self._dsn,
            connect_timeout=self._config.connect_timeout_seconds,
            application_name=self._config.application_name,
            autocommit=False,
        )

    async def explain_json(
        self, sql: str, parameters: Sequence[Any] | None, *, timeout_ms: int
    ) -> list[dict[str, Any]]:
        try:
            conn = await self._connect()
        except Exception as exc:
            raise PlanUnavailable(redact_exception(exc)) from None

        try:
            async with conn:
                async with conn.cursor() as cur:
                    # Transaction-scoped: cannot leak into another session.
                    await cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
                    await cur.execute("SET LOCAL transaction_read_only = on")
                    # FORMAT JSON only. ANALYZE is never added here.
                    await cur.execute(f"EXPLAIN (FORMAT JSON) {sql}", parameters or None)
                    row = await cur.fetchone()
                    if not row or not row[0]:
                        raise PlanUnavailable("empty_plan")
                    return row[0]
        except PlanUnavailable:
            raise
        except Exception as exc:
            # Driver messages routinely embed the failing statement.
            raise PlanUnavailable(redact_exception(exc)) from None

    async def stats_freshness_hours(self) -> float | None:
        """Narrow, reviewed metadata read.

        Deliberately an aggregate over ``pg_stat_user_tables`` rather than
        SELECT access to every user table.
        """
        query = (
            "SELECT EXTRACT(EPOCH FROM (now() - "
            "MIN(GREATEST(COALESCE(last_analyze, 'epoch'::timestamptz), "
            "COALESCE(last_autoanalyze, 'epoch'::timestamptz))))) / 3600.0 "
            "FROM pg_stat_user_tables"
        )
        try:
            conn = await self._connect()
            async with conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"SET LOCAL statement_timeout = {self._config.statement_timeout_ms}"
                    )
                    await cur.execute(query)
                    row = await cur.fetchone()
                    return float(row[0]) if row and row[0] is not None else None
        except Exception as exc:
            logger.debug("stats freshness unavailable: %s", redact_exception(exc))
            return None


class StaticPlanSource:
    """Fake plan source for tests and fixtures."""

    def __init__(
        self,
        plan: list[dict[str, Any]] | None = None,
        *,
        raise_unavailable: str | None = None,
        stats_hours: float | None = 1.0,
    ) -> None:
        self._plan = plan or []
        self._raise = raise_unavailable
        self._stats_hours = stats_hours
        self.calls: list[str] = []

    async def explain_json(
        self, sql: str, parameters: Sequence[Any] | None, *, timeout_ms: int
    ) -> list[dict[str, Any]]:
        # Recorded only so tests can assert the estimator never executed the
        # candidate. The statement is not retained beyond the test process.
        self.calls.append("explain")
        if self._raise:
            raise PlanUnavailable(self._raise)
        return self._plan

    async def stats_freshness_hours(self) -> float | None:
        return self._stats_hours


def _walk(node: dict[str, Any]):
    """Depth-first walk over every plan node, including sub-plans and CTEs."""
    yield node
    for key in ("Plans", "Subplans"):
        for child in node.get(key, []) or []:
            yield from _walk(child)


class PlanEstimator:
    """Produces a sanitised ``PlanEstimate`` from a validated statement."""

    def __init__(
        self,
        source: PlanSource,
        config: PlanEstimatorConfig | None = None,
        *,
        result_source: Source = Source.LIVE,
    ) -> None:
        self._source = source
        self._config = config or PlanEstimatorConfig()
        self._result_source = result_source

    async def estimate(
        self,
        validated_sql: str,
        parameters: Sequence[Any] | None,
        target_id: str,
        *,
        request_id: str | None = None,
    ) -> PlanEstimate:
        """Estimate cost without executing the candidate.

        ``validated_sql`` and ``parameters`` are transient trusted inputs. They
        are used for the EXPLAIN call and then go out of scope; neither is stored
        on the returned object, logged, or included in any error path.
        """
        rid = request_id or new_id("req")
        captured_at = now_utc()

        try:
            raw_plan = await self._source.explain_json(
                validated_sql,
                parameters,
                timeout_ms=self._config.statement_timeout_ms,
            )
        except PlanUnavailable as exc:
            return self._unavailable(rid, captured_at, exc.reason)
        except Exception as exc:
            return self._unavailable(rid, captured_at, redact_exception(exc))

        try:
            estimate = self._extract(raw_plan, rid, captured_at)
        except Exception as exc:
            return self._unavailable(rid, captured_at, redact_exception(exc))
        finally:
            # Explicit: the raw plan is not retained past this method.
            raw_plan = None  # noqa: F841

        stats_hours = await self._source.stats_freshness_hours()
        return estimate.model_copy(
            update={
                "stats_quality": self._stats_quality(stats_hours),
                "uncertainty": estimate.uncertainty + self._stats_notes(stats_hours),
            }
        )

    # -- internals ---------------------------------------------------------

    def _unavailable(self, request_id: str, captured_at, reason: str) -> PlanEstimate:
        """Explicit unknown. Gautam's policy decides whether to review or block.

        Returning zeros here would be the single most dangerous bug in this
        module: an unknown-cost query would look free.
        """
        return PlanEstimate(
            request_id=request_id,
            source=self._result_source,
            captured_at=captured_at,
            plan_source="unavailable",
            stats_quality="unknown",
            uncertainty=[f"plan estimate unavailable ({reason}); cost is unknown"],
            estimation_error=reason,
        )

    def _extract(
        self, raw_plan: list[dict[str, Any]], request_id: str, captured_at
    ) -> PlanEstimate:
        if not raw_plan:
            raise PlanUnavailable("empty_plan")

        root = raw_plan[0].get("Plan")
        if not isinstance(root, dict):
            raise PlanUnavailable("malformed_plan")

        rows = float(root.get("Plan Rows", 0) or 0)
        width = float(root.get("Plan Width", 0) or 0)
        cost = float(root.get("Total Cost", 0) or 0)

        categories: list[str] = []
        max_node_rows = rows
        uncertainty: list[str] = []
        saw_limit = False
        blocking_under_limit = False

        for node in _walk(root):
            node_type = str(node.get("Node Type", "")).strip()
            if node_type and node_type not in categories:
                categories.append(node_type)

            node_rows = float(node.get("Plan Rows", 0) or 0)
            if node_type in SCAN_NODE_TYPES or node_type in BLOCKING_NODE_TYPES:
                max_node_rows = max(max_node_rows, node_rows)

            if node_type == "Limit":
                saw_limit = True
                for descendant in _walk(node):
                    if descendant is node:
                        continue
                    if str(descendant.get("Node Type", "")) in BLOCKING_NODE_TYPES:
                        blocking_under_limit = True

        output_bytes = rows * width * self._config.serialization_overhead_factor

        uncertainty.append(
            "estimated_output_bytes assumes a "
            f"{self._config.serialization_overhead_factor:.2f}x wire/serialisation "
            "overhead above raw row width; measured bytes will differ"
        )
        uncertainty.append(
            "planner_cost is an abstract PostgreSQL cost unit and is not a duration"
        )

        if max_node_rows > rows * 10 and rows > 0:
            uncertainty.append(
                f"a plan node estimates {max_node_rows:.0f} rows while the statement "
                f"returns {rows:.0f}; resource risk follows the scanned node, not the "
                "output row count"
            )
        elif rows <= 1 and max_node_rows > 1:
            uncertainty.append(
                f"top-level output is {rows:.0f} row(s) but a node estimates "
                f"{max_node_rows:.0f} rows scanned; an aggregate can be expensive "
                "while returning almost nothing"
            )

        if saw_limit and blocking_under_limit:
            uncertainty.append(
                "a blocking node (sort/hash/aggregate) sits beneath a LIMIT; the LIMIT "
                "does not reduce the work that node performs"
            )

        return PlanEstimate(
            request_id=request_id,
            source=self._result_source,
            captured_at=captured_at,
            plan_source="explain_json",
            estimated_rows=rows,
            estimated_width_bytes=width,
            estimated_output_bytes=round(output_bytes, 2),
            planner_cost=cost,
            max_node_estimated_rows=max_node_rows,
            node_categories=categories,
            stats_quality="unknown",
            uncertainty=uncertainty,
        )

    def _stats_quality(self, hours: float | None) -> str:
        if hours is None:
            return "unknown"
        return "stale" if hours > self._config.stats_stale_after_hours else "fresh"

    def _stats_notes(self, hours: float | None) -> list[str]:
        if hours is None:
            return ["planner statistics freshness could not be determined"]
        if hours > self._config.stats_stale_after_hours:
            return [
                f"planner statistics are ~{hours:.1f}h old (threshold "
                f"{self._config.stats_stale_after_hours:.0f}h); estimates may be well off"
            ]
        return []
