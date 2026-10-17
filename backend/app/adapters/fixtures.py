"""Synthetic development/test adapters; no cluster or SQL access."""
import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from app.contracts import *
from app.api.ports import Ports
from app.api.errors import PortError
from app.api.evidence import digest, summary_hash

ROOT = Path(__file__).resolve().parents[3]
def now(): return datetime.now(timezone.utc)
def new_id(): return str(uuid4())
def load(name, cls): return cls.model_validate_json((ROOT / "fixtures/v1" / (name + ".json")).read_text(encoding="utf-8"))

class FixturePlatform:
    async def get_topology(self, sandbox_id): return load("topology", TopologySnapshot)
    async def get_target(self, target_id):
        target = load("target", VerifiedTarget)
        if target_id != target.target_id: raise PortError("NOT_FOUND", 404)
        return target

class FixtureTelemetry:
    async def get_state(self, sandbox_id):
        return load("state", SystemStateSnapshot).model_copy(update={"captured_at": now(), "valid_until": now() + timedelta(seconds=30)})
    async def begin_phase(self, run_id, phase, load_profile): return phase
    async def close_phase(self, phase_handle):
        return next(p for p in load("summary", RunSummary).phase_summaries if p.phase == phase_handle)
    async def finalize_run(self, run_id, runtime_facts):
        summary = load("summary", RunSummary).model_copy(update={"run_id": run_id, **runtime_facts})
        return summary.model_copy(update={"evidence_hash": summary_hash(summary)})

class FixtureIntelligence:
    def __init__(self): self.started = now()
    async def plan(self, topology, state, catalog, coverage):
        return [load("proposal", ExperimentProposal).model_copy(update={"expires_at": now() + timedelta(seconds=60)})]
    async def analyze_run(self, summary, topology):
        a = load("analysis", AnalysisResult)
        refs = [r.model_copy(update={"run_id": summary.run_id, "evidence_hash": summary.evidence_hash}) for r in a.evidence_refs]
        return a.model_copy(update={"run_id": summary.run_id, "evidence_refs": refs, "hypotheses": [f.model_copy(update={"evidence_refs": refs}) for f in a.hypotheses], "recommendations": [f.model_copy(update={"evidence_refs": refs}) for f in a.recommendations]})
    async def observe(self, observation): return await self.get_learning_status()
    async def get_learning_status(self): return load("learning", LearningStatus).model_copy(update={"started_at": self.started})

class FakeRuntime:
    """Approval/lifecycle test double; never a live safety worker."""
    def __init__(self, telemetry, enabled=False, delay=0.05):
        self.enabled, self.delay, self.telemetry = enabled, delay, telemetry
        self.proposals, self.approvals, self.runs, self.histories, self.keys = {}, {}, {}, {}, {}
        self.tasks, self.latched, self.active = set(), False, None
        s = load("summary", RunSummary)
        h = RunHandle(source="fixture", run_id=s.run_id, proposal_id="fixture-completed", state="COMPLETED", accepted_at=now(), status_url=f"/api/v1/runs/{s.run_id}", events_url=f"/api/v1/runs/{s.run_id}/events")
        self.runs[s.run_id] = RunStatus(source="fixture", handle=h, cleanup_status=s.cleanup_status, recovery_status=s.recovery_status, summary=s)
        self.histories[s.run_id] = deque(maxlen=128)
    def require_enabled(self):
        if not self.enabled: raise PortError("MUTATIONS_DISABLED")
    async def catalog(self):
        return Catalog(source="fixture", entries=[CatalogEntry(catalog_id=id, name=name, mechanism=mechanism, available=self.enabled and id == "EXP-00", max_duration_seconds=60, reason="Synthetic only; no live runtime installed") for id, name, mechanism in [("EXP-00", "Service pause", "scale to zero"), ("EXP-01", "Pod termination", "delete one authorized pod"), ("EXP-02", "Redis interruption", "TCP interruption"), ("EXP-03", "Network latency", "bounded TCP delay")]])
    async def validate(self, p):
        good = p.source == "fixture" and p.sandbox_id == "fixture-sandbox" and p.target_id == "cartservice" and p.catalog_id == "EXP-00" and p.expires_at > now() and not self.latched
        if p.proposal_id not in self.proposals and len(self.proposals) >= 128: raise PortError("RATE_LIMIT", 429)
        if good: self.proposals[p.proposal_id] = p.model_copy(deep=True)
        return ValidationResult(proposal_id=p.proposal_id, proposal_hash=digest(p), valid=good, checks=[ValidationCheck(name="fixture_identity_and_expiry", passed=good, reason="Fake adapter only")], rejection_codes=[] if good else ["FIXTURE_VALIDATION_FAILED"], sandbox_id=p.sandbox_id, target_id=p.target_id, policy_version=p.policy_version, expires_at=p.expires_at)
    async def approve(self, proposal_id, actor, proposal_hash):
        self.require_enabled()
        if self.latched: raise PortError("STOP_LATCHED")
        p = self.proposals.get(proposal_id)
        if not p: raise PortError("APPROVAL_REQUIRED")
        if p.expires_at <= now(): raise PortError("EXPIRED")
        if digest(p) != proposal_hash: raise PortError("HASH_MISMATCH")
        if len(self.approvals) >= 128: raise PortError("RATE_LIMIT", 429)
        a = ApprovalRecord(approval_id=new_id(), proposal_hash=proposal_hash, actor_ref=actor, approved_at=now(), expires_at=min(p.expires_at, now() + timedelta(seconds=60)), scope="experiment")
        self.approvals[a.approval_id] = a
        return a
    async def start(self, proposal_id, approval_id, idempotency_key, actor):
        self.require_enabled()
        fingerprint = (proposal_id, approval_id, actor)
        if idempotency_key in self.keys:
            old, rid = self.keys[idempotency_key]
            if old != fingerprint: raise PortError("CONFLICT")
            return self.runs[rid].handle
        if self.latched: raise PortError("STOP_LATCHED")
        if self.active or len(self.runs) >= 128 or len(self.keys) >= 128: raise PortError("CONFLICT")
        p, a = self.proposals.get(proposal_id), self.approvals.get(approval_id)
        if not p or not a or a.actor_ref != actor or a.proposal_hash != digest(p): raise PortError("APPROVAL_REQUIRED")
        if a.expires_at <= now() or p.expires_at <= now(): raise PortError("EXPIRED")
        self.approvals.pop(approval_id)
        rid = new_id()
        h = RunHandle(source="fixture", run_id=rid, proposal_id=proposal_id, state="BASELINING", accepted_at=now(), status_url=f"/api/v1/runs/{rid}", events_url=f"/api/v1/runs/{rid}/events")
        self.runs[rid] = RunStatus(source="fixture", handle=h, cleanup_status="pending", recovery_status="pending", summary=None)
        self.histories[rid], self.active = deque(maxlen=128), rid
        self.keys[idempotency_key] = (fingerprint, rid)
        self.emit(rid, "BASELINING")
        task = asyncio.create_task(self.progress(rid)); self.tasks.add(task); task.add_done_callback(self.tasks.discard)
        return h
    def emit(self, rid, state):
        status, history = self.runs[rid], self.histories[rid]
        status.handle = status.handle.model_copy(update={"state": state})
        sequence = history[-1].sequence + 1 if history else 1
        history.append(RunEvent(source="fixture", run_id=rid, sequence=sequence, timestamp=now(), event_type="transition", state=state, details={"note": "Synthetic lifecycle; no Kubernetes operation"}))
    async def progress(self, rid):
        try:
            for state in ["PREFLIGHT", "INJECTING", "OBSERVING", "CLEANING_UP", "VERIFYING_RECOVERY", "ANALYZING", "COMPLETED"]:
                await asyncio.sleep(self.delay)
                if self.runs[rid].handle.state == "CLEANING_UP" and state in ["PREFLIGHT", "INJECTING", "OBSERVING"]: continue
                self.emit(rid, state)
            status = self.runs[rid]
            status.summary = await self.telemetry.finalize_run(rid, {})
            status.cleanup_status, status.recovery_status = "verified", "verified"
        finally: self.active = None
    async def status(self, rid):
        if rid not in self.runs: raise PortError("NOT_FOUND", 404)
        return self.runs[rid]
    async def events(self, rid, after_sequence):
        await self.status(rid)
        return [e for e in self.histories[rid] if e.sequence > after_sequence]
    async def stop(self, rid, actor):
        self.require_enabled(); status = await self.status(rid)
        if status.summary is None: self.emit(rid, "CLEANING_UP")
        return StopReceipt(source="fixture", request_id=new_id(), accepted_at=now(), stop_latched=self.latched, affected_run_ids=[rid], cleanup_status=status.cleanup_status)
    async def emergency_stop(self, sandbox_id, actor):
        self.latched = True
        ids = [self.active] if self.active else []
        if self.active: self.emit(self.active, "CLEANING_UP")
        return StopReceipt(source="fixture", request_id=new_id(), accepted_at=now(), stop_latched=True, affected_run_ids=ids, cleanup_status="pending" if ids else "not_applicable")
    async def reconcile(self, sandbox_id):
        return CleanupSummary(source="fixture", sandbox_id=sandbox_id, cleanup_status="pending" if self.active else "verified", recovery_status="pending" if self.active else "verified", unresolved_run_ids=[self.active] if self.active else [])
    async def reset(self, sandbox_id, actor):
        r = await self.reconcile(sandbox_id)
        if r.unresolved_run_ids: raise PortError("CONFLICT")
        self.latched = False; return r

def fixture_ports(enabled=False, delay=0.05):
    t = FixtureTelemetry()
    return Ports(FixturePlatform(), t, FixtureIntelligence(), FakeRuntime(t, enabled, delay))
