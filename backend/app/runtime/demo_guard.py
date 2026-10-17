import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from app.api.errors import PortError
from app.contracts import CleanupSummary, StopReceipt
from .constants import (
    EVIDENCE_LABEL,
    LOCAL_DEMO_TARGET_HEALTH_URL,
    MAX_FAULT_DURATION_SECONDS,
    POPUP_BODY,
    POPUP_HEADING,
)
from .demo_contract import DemoInjectionRequest, DemoInjectionResult
from .executor import MutationExecutor
from .health import LoopbackHealthClient, verify_health_for_reset, is_probe_stale
from .latch import StopLatchStore
from .preflight import evaluate_preflight


def _now() -> datetime:
    return datetime.now(timezone.utc)


def default_latch_path() -> Path:
    override = os.getenv("DEMO_LATCH_FILE")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "work" / "demo_stop_latch.json"


class DemoRuntimeGuard:
    """Deterministic preflight, STOP latch, and reset for the local prevention demo."""

    def __init__(
        self,
        *,
        health_client: LoopbackHealthClient | None = None,
        executor: MutationExecutor | None = None,
        latch_store: StopLatchStore | None = None,
        sandbox_id: str = "local-demo",
    ) -> None:
        self.health = health_client or LoopbackHealthClient(LOCAL_DEMO_TARGET_HEALTH_URL)
        self.executor = executor or MutationExecutor()
        self.latch_store = latch_store or StopLatchStore(default_latch_path())
        self.sandbox_id = sandbox_id
        self.started = _now()
        self.last_result: DemoInjectionResult | None = None
        self.probes = 0
        self.failed = 0
        state = self.latch_store.load()
        self.latched = state.latched

    async def probe(self) -> dict:
        self.probes += 1
        result = await self.health.probe_fresh()
        status = result.status
        if result.stale or is_probe_stale(result):
            status = "unavailable"
        if status != "healthy":
            self.failed += 1
        return {"status": status, "requests": result.requests}

    def _latch_stop(self, request_id: str) -> None:
        self.latched = True
        self.latch_store.save(latched=True, request_id=request_id)

    async def inject_attempt(self, body: DemoInjectionRequest, actor: str) -> DemoInjectionResult:
        probe = await self.probe()
        outcome = evaluate_preflight(
            body=body,
            latched=self.latched,
            target_status=probe["status"],
        )
        request_id = str(uuid4())
        if not outcome.passed:
            self._latch_stop(request_id)
        heading = "Safety check passed" if outcome.passed else POPUP_HEADING
        popup_body = "Duration accepted for demo" if outcome.passed else POPUP_BODY
        result = DemoInjectionResult(
            source="live",
            request_id=request_id,
            captured_at=_now(),
            decision="ALLOW" if outcome.passed else "BLOCK",
            evidence_label=EVIDENCE_LABEL,
            popup_heading=heading,
            popup_body=popup_body,
            message=f"{heading} — {popup_body}.",
            reason=outcome.reason,
            rule_id=outcome.rule_id,
            target_status=probe["status"],
            target_requests=probe["requests"],
            duration_seconds=body.duration_seconds,
            maximum_seconds=MAX_FAULT_DURATION_SECONDS,
            stop_latched=self.latched,
        )
        self.last_result = result
        return result

    async def emergency_stop(self, sandbox_id: str, actor: str) -> StopReceipt:
        self._latch_stop(str(uuid4()))
        return StopReceipt(
            source="live",
            request_id=str(uuid4()),
            accepted_at=_now(),
            stop_latched=True,
            affected_run_ids=[],
            cleanup_status="not_applicable",
        )

    async def reconcile(self, sandbox_id: str) -> CleanupSummary:
        probe = await self.probe()
        return CleanupSummary(
            source="live",
            sandbox_id=sandbox_id,
            cleanup_status="not_applicable",
            recovery_status="verified" if probe["status"] == "healthy" else "unknown",
            unresolved_run_ids=[],
        )

    async def reset(self, sandbox_id: str, actor: str) -> CleanupSummary:
        await verify_health_for_reset(self.health)
        self.latch_store.clear()
        self.latched = False
        self.last_result = None
        return CleanupSummary(
            source="live",
            sandbox_id=sandbox_id,
            cleanup_status="not_applicable",
            recovery_status="verified",
            unresolved_run_ids=[],
        )
