"""Read-only import of sanitized, hash-checked recorded evidence."""
from pathlib import Path
from app.contracts import *
from app.api.ports import Ports
from app.api.evidence import checked_evidence, summary_hash
from app.api.errors import PortError

class ReplayBundle(Contract):
    recorded_from: Literal["live"]
    topology: TopologySnapshot
    state: SystemStateSnapshot
    learning: LearningStatus
    summary: RunSummary
    analysis: AnalysisResult
    events: list[RunEvent] = Field(max_length=128)

class ReplayAdapter:
    def __init__(self, bundle):
        self.b = bundle
    async def get_topology(self, sandbox_id): return self.b.topology
    async def get_state(self, sandbox_id): return self.b.state
    async def get_learning_status(self): return self.b.learning
    async def analyze_run(self, summary, topology): return self.b.analysis
    async def catalog(self): return Catalog(source="replay", entries=[])
    async def plan(self, topology, state, catalog, coverage): return []
    async def status(self, run_id):
        if run_id != self.b.summary.run_id: raise PortError("NOT_FOUND", 404)
        s = self.b.summary
        return RunStatus(source="replay", handle=RunHandle(source="replay", run_id=run_id, proposal_id="recorded", state="COMPLETED", accepted_at=s.phase_summaries[0].started_at, status_url=f"/api/v1/runs/{run_id}", events_url=f"/api/v1/runs/{run_id}/events"), cleanup_status=s.cleanup_status, recovery_status=s.recovery_status, summary=s)
    async def events(self, run_id, after_sequence):
        await self.status(run_id)
        return [e for e in self.b.events if e.sequence > after_sequence]

def replay_ports(path: Path):
    if path.stat().st_size > 1048576: raise ValueError("Replay bundle exceeds 1 MiB")
    b = ReplayBundle.model_validate_json(path.read_text(encoding="utf-8"))
    objects = [b.topology, b.state, b.learning, b.summary, b.analysis, *b.events]
    if any(x.source != "replay" for x in objects): raise ValueError("Replay objects must be explicitly labelled replay")
    if not b.summary.phase_summaries: raise ValueError("Replay needs actual phase windows")
    checked_evidence(b.summary, b.analysis)
    a = ReplayAdapter(b)
    return Ports(a, a, a, a)
