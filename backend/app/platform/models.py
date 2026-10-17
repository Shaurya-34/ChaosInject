"""Small platform-owned v1 models; kept independent of API frameworks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class SandboxIdentity:
    sandbox_id: str
    context: str
    api_endpoint: str
    ca_fingerprint: str
    namespace_uids: dict[str, str]
    enrolled_at: str
    schema_version: str = "1.0"


@dataclass(frozen=True)
class TopologyNode:
    resource_id: str
    kind: str
    namespace: str
    name: str
    uid: str | None
    selector: dict[str, str] = field(default_factory=dict)
    ports: tuple[dict[str, Any], ...] = ()
    desired_replicas: int | None = None
    ready_replicas: int | None = None
    stateful: bool | None = None
    confidence: float = 1.0


@dataclass(frozen=True)
class TopologyEdge:
    caller: str
    dependency: str
    protocol: str | None
    evidence: str
    source_ref: str
    confidence: float


@dataclass(frozen=True)
class TopologySnapshot:
    graph_id: str
    upstream_sha: str | None
    generated_at: str
    sandbox_id: str
    namespace: str
    nodes: tuple[TopologyNode, ...]
    edges: tuple[TopologyEdge, ...]
    manual_corrections: tuple[dict[str, Any], ...] = ()
    source: Literal["live", "fixture", "replay"] = "fixture"
    schema_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VerifiedTarget:
    target_id: str
    sandbox_id: str
    namespace: str
    kind: str
    name: str
    uid: str
    selector: dict[str, str]
    allowed_fault_ids: tuple[str, ...]
    dependency_refs: tuple[str, ...]
    provenance: str
    schema_version: str = "1.0"


class SandboxMismatch(ValueError):
    """Raised when a request does not match the explicitly enrolled sandbox."""


def affected_callers(snapshot: TopologySnapshot, failed_dependency: str) -> list[str]:
    """Return transitive callers using the caller -> dependency edge direction."""
    reverse: dict[str, set[str]] = {}
    for edge in snapshot.edges:
        reverse.setdefault(edge.dependency, set()).add(edge.caller)
    found: set[str] = set()
    frontier = [failed_dependency]
    while frontier:
        current = frontier.pop()
        for caller in reverse.get(current, set()):
            if caller not in found:
                found.add(caller)
                frontier.append(caller)
    return sorted(found)
