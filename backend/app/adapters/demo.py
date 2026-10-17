"""Local prevention demo adapter — no Kubernetes mutation capability."""
import os
from datetime import timedelta
from uuid import uuid4
from app.contracts import *
from app.api.ports import Ports
from app.api.errors import PortError
from app.runtime.demo_contract import DemoInjectionRequest, DemoInjectionResult
from app.runtime.demo_guard import DemoRuntimeGuard, _now
from app.runtime.health import LoopbackHealthClient

# Re-export for API composition imports.
__all__ = ["DemoInjectionRequest", "DemoInjectionResult", "DemoAdapter", "demo_ports"]


class DemoAdapter:
    def __init__(self, settings):
        self.settings = settings
        target_port = int(os.getenv("DEMO_TARGET_PORT", "8090"))
        if not 1 <= target_port <= 65535: raise ValueError("Invalid loopback target port")
        self.guard = DemoRuntimeGuard(sandbox_id="local-demo", health_client=LoopbackHealthClient(f"http://127.0.0.1:{target_port}/health"))
        self.started = self.guard.started

    @property
    def latched(self) -> bool:
        return self.guard.latched

    @property
    def last_result(self) -> DemoInjectionResult | None:
        return self.guard.last_result

    async def probe(self):
        return await self.guard.probe()

    async def get_state(self, sandbox_id):
        p = await self.probe()
        return SystemStateSnapshot(
            source="live",
            state_id=str(uuid4()),
            captured_at=_now(),
            valid_until=_now() + timedelta(seconds=5),
            sandbox_id="local-demo",
            ready_by_workload={"demo-target": Readiness(ready=1 if p["status"] == "healthy" else 0, desired=1)},
            request_rate=None,
            error_rate=self.guard.failed / max(self.guard.probes, 1),
            p95_ms=None,
            db_connections_used=None,
            db_connections_limit=None,
            missing_features=[
                "request_rate: rate collector not connected",
                "p95_ms: percentile collector not connected",
                "db_connections_used: no SQL lab",
                "db_connections_limit: no SQL lab",
                "cpu_utilization: no collector",
                "memory_utilization: no collector",
            ],
            active_fault_id=None,
        )

    async def get_topology(self, sandbox_id):
        return TopologySnapshot(
            source="live",
            graph_id="local-demo-graph",
            upstream_sha=None,
            generated_at=_now(),
            sandbox_id="local-demo",
            namespace="local-http",
            nodes=[
                TopologyNode(
                    target_id=n,
                    name=n,
                    kind="Service",
                    uid="local-" + n,
                    selector={},
                    ports=[p],
                    desired_replicas=1,
                    stateful=False,
                    confidence=1,
                )
                for n, p in [("guard-api", 8000), ("demo-target", 8090)]
            ],
            edges=[
                TopologyEdge(
                    caller="guard-api",
                    dependency="demo-target",
                    protocol="HTTP",
                    provenance="configured fixed loopback health route",
                    confidence=1,
                )
            ],
            manual_corrections=["Local service identities; no Kubernetes discovery or UIDs"],
        )

    async def get_learning_status(self):
        return LearningStatus(
            source="live",
            learner_version="not-integrated",
            started_at=self.started,
            processed=0,
            updates=0,
            pending_count=0,
            dropped_events=0,
            drift_status="cold_start",
            prediction_error=None,
            queue_limit=256,
            pending_limit=128,
            metric_buffer_seconds=120,
            intent_ttl_seconds=60,
            model_limit_bytes=0,
        )

    async def catalog(self):
        return Catalog(source="live", entries=[])

    async def inject_attempt(self, body, actor):
        return await self.guard.inject_attempt(body, actor)

    async def emergency_stop(self, sandbox_id, actor):
        return await self.guard.emergency_stop(sandbox_id, actor)

    async def reconcile(self, sandbox_id):
        return await self.guard.reconcile(sandbox_id)

    async def reset(self, sandbox_id, actor):
        return await self.guard.reset(sandbox_id, actor)

    async def status(self, run_id):
        raise PortError("NOT_FOUND", 404)

    async def events(self, run_id, after_sequence):
        raise PortError("NOT_FOUND", 404)


def demo_ports(settings):
    from app.intelligence.service import IntelligenceService
    adapter = DemoAdapter(settings)
    intelligence = IntelligenceService()
    return Ports(adapter, adapter, intelligence, adapter)
