"""Fake adapters and SYNTHETIC inputs for intelligence tests and fixtures.

Nothing here is a measurement. The topology mirrors the vendored Online
Boutique call graph but is not discovered from a cluster.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from app.api.evidence import summary_hash
from app.contracts import (
    Catalog,
    CatalogEntry,
    LoadProfile,
    OperationAggregate,
    PhaseSummary,
    Readiness,
    RunSummary,
    ScoreComponents,
    SystemStateSnapshot,
    TopologyEdge,
    TopologyNode,
    TopologySnapshot,
)

SANDBOX_ID = "fixture-sandbox"


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------

OB_SERVICES = (
    "frontend", "adservice", "recommendationservice", "productcatalogservice", "cartservice", "redis-cart",
    "shippingservice", "currencyservice", "checkoutservice", "paymentservice", "emailservice",
)
OB_EDGES = (
    ("frontend", "adservice"), ("frontend", "recommendationservice"), ("frontend", "productcatalogservice"),
    ("frontend", "cartservice"), ("frontend", "shippingservice"), ("frontend", "currencyservice"),
    ("frontend", "checkoutservice"), ("recommendationservice", "productcatalogservice"), ("cartservice", "redis-cart"),
    ("checkoutservice", "productcatalogservice"), ("checkoutservice", "shippingservice"),
    ("checkoutservice", "paymentservice"), ("checkoutservice", "emailservice"), ("checkoutservice", "currencyservice"),
    ("checkoutservice", "cartservice"),
)


def topology(now: datetime | None = None, *, injected_name: str | None = None) -> TopologySnapshot:
    now = now or _now()
    nodes = [
        TopologyNode(
            target_id=name,
            name=injected_name if (injected_name and name == "adservice") else name,
            kind="Deployment",
            uid=f"uid-{name}",
            selector={"app": name},
            ports=[6379] if name == "redis-cart" else [8080],
            desired_replicas=1,
            stateful=name == "redis-cart",
            confidence=1.0,
        )
        for name in OB_SERVICES
    ]
    edges = [
        TopologyEdge(
            caller=c, dependency=d, protocol="redis" if d == "redis-cart" else "grpc",
            provenance="fixture: vendored upstream manifests", confidence=0.8 if (c, d) == ("checkoutservice", "emailservice") else 0.95,
        )
        for c, d in OB_EDGES
    ]
    return TopologySnapshot(
        source="fixture", graph_id=_id("graph"), upstream_sha=None, generated_at=now, sandbox_id=SANDBOX_ID,
        namespace="boutique", nodes=nodes, edges=edges, manual_corrections=[],
    )


def state(
    now: datetime | None = None,
    *,
    stale: bool = False,
    db_used: int | None = 4,
    db_limit: int | None = 50,
    error_rate: float | None = 0.0,
    active_fault_id: str | None = None,
    not_ready: tuple[str, ...] = (),
    source: str = "fixture",
) -> SystemStateSnapshot:
    now = now or _now()
    captured = now - timedelta(seconds=120 if stale else 2)
    missing = [f"{name}: fixture metric withheld" for name, v in (("db_connections_used", db_used), ("db_connections_limit", db_limit), ("error_rate", error_rate)) if v is None]
    return SystemStateSnapshot(
        source=source,  # type: ignore[arg-type]
        state_id=_id("state"),
        captured_at=captured,
        valid_until=captured + timedelta(seconds=30),
        sandbox_id=SANDBOX_ID,
        ready_by_workload={n: Readiness(ready=0 if n in not_ready else 1, desired=1) for n in OB_SERVICES},
        request_rate=12.0,
        error_rate=error_rate,
        p95_ms=180.0,
        db_connections_used=db_used,
        db_connections_limit=db_limit,
        missing_features=missing + ["cpu_utilization: not collected", "memory_utilization: not collected"],
        active_fault_id=active_fault_id,
    )


def catalog(available: bool = True, unavailable: tuple[str, ...] = ("EXP-03",)) -> Catalog:
    rows = [("EXP-00", "Service pause", "scale to zero"), ("EXP-01", "Pod termination", "delete one authorized pod"),
            ("EXP-02", "Redis interruption", "TCP interruption"), ("EXP-03", "Network latency", "bounded TCP delay")]
    return Catalog(
        source="fixture",
        entries=[
            CatalogEntry(catalog_id=cid, name=name, mechanism=mech, available=available and cid not in unavailable,
                         max_duration_seconds=60, reason="Synthetic fixture catalog")
            for cid, name, mech in rows
        ],
    )


OPERATIONS = ("browse", "product_view", "add_to_cart", "cart_read", "checkout")


def agg(attempted: int, failed: int, p50: float, p95: float) -> OperationAggregate:
    succeeded = attempted - failed
    return OperationAggregate(
        attempted=attempted, succeeded=succeeded, failed=failed,
        error_categories={"http_5xx": failed} if failed else {},
        success_rate=succeeded / attempted if attempted else None,
        p50_ms=p50, p95_ms=p95, percentile_method="nearest-rank", sample_count=attempted, missing=[],
    )


def run_summary(
    now: datetime | None = None,
    *,
    catalog_id: str = "EXP-00",
    fault_ops: dict[str, OperationAggregate] | None = None,
    recovery_ops: dict[str, OperationAggregate] | None = None,
    recovery_status: str = "verified",
    recovery_seconds: float | None = 18.0,
    cleanup_status: str = "verified",
    samples: int = 120,
    source: str = "fixture",
) -> RunSummary:
    """SYNTHETIC run summary. Default fault: productcatalogservice paused (every hard path fails)."""

    now = now or _now()
    t0 = now - timedelta(minutes=5)
    baseline = {op: agg(samples, 0, 60, 150) for op in OPERATIONS}
    fault = fault_ops or {op: agg(samples, samples // 2, 80, 900) for op in OPERATIONS}
    recovery = recovery_ops or {op: agg(samples, 0, 62, 160) for op in OPERATIONS}

    def phase(name, start, end, ops):
        return PhaseSummary(
            phase=name, started_at=t0 + timedelta(seconds=start), ended_at=t0 + timedelta(seconds=end), operations=ops,
            offered_rate=10.0, achieved_rate=10.0, ready_min=0 if name == "fault" else 1, desired_max=1,
            quality_notes=["SYNTHETIC fixture numbers"], percentile_method="nearest-rank",
        )

    summary = RunSummary(
        source=source,  # type: ignore[arg-type]
        run_id=_id("run"),
        catalog_id=catalog_id,
        sandbox_id=SANDBOX_ID,
        topology_hash="fixture",
        application_revision=None,
        load_profile=LoadProfile(
            load_profile_id="steady-browse-cart-checkout-v1", operation_mix={op: 0.2 for op in OPERATIONS}, concurrency=2,
            requested_rate=10.0, timeout_seconds=2.0, warmup_seconds=10.0, measurement_seconds=60.0,
            generator_version="fixture-no-generator",
        ),
        phase_summaries=[phase("baseline", 0, 60, baseline), phase("fault", 60, 90, fault), phase("recovery", 90, 150, recovery)],
        timeline_markers=[],
        stop_reason="completed",
        cleanup_status=cleanup_status,  # type: ignore[arg-type]
        recovery_status=recovery_status,  # type: ignore[arg-type]
        recovery_seconds=recovery_seconds,
        data_quality=["SYNTHETIC fixture numbers"],
        score=ScoreComponents(availability=None, latency=None, recovery=None, safety=None, total=None,
                              configuration="fixture", missing=["synthetic fixture: no score computed"]),
        evidence_hash="",
    )
    return summary.model_copy(update={"evidence_hash": summary_hash(summary)})
