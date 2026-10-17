"""Module-private working types.

Shared contract types come only from ``app.contracts``. These dataclasses hold
richer intermediate state (rule templates, evidence pointers, severities) and
are converted to the shared contracts at the module boundary. They are never
exported over the API.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

FindingStatus = Literal["predicted", "supported_in_this_run", "not_supported", "insufficient_evidence"]


@dataclass(frozen=True)
class QueryRecommendation:
    template_id: str
    summary: str
    changes_result_set: bool
    caveats: tuple[str, ...] = ()

    def render(self) -> str:
        effect = "changes the result set" if self.changes_result_set else "does not change the result set"
        caveats = " ".join(self.caveats)
        return f"{self.template_id}: {self.summary} ({effect}; analysed again as a new request.)" + (
            f" Caveats: {caveats}" if caveats else ""
        )


@dataclass(frozen=True)
class QueryRisk:
    """Deterministic heuristic risk, 0-100. Not a crash probability."""

    score: float
    components: dict[str, float]

    def render(self) -> str:
        return f"Heuristic risk {self.score:.1f}/100 (rule-based heuristic, not a probability of failure)."


@dataclass(frozen=True)
class Fact:
    fact_id: str
    statement: str
    value: float | str | None
    unit: str | None
    pointer: str  # RFC 6901 pointer into RunSummary.model_dump(mode="json")

    def render(self) -> str:
        unit = f" {self.unit}" if self.unit and self.value is not None else ""
        return f"{self.statement}: {self.value}{unit} [{self.pointer}]"


@dataclass(frozen=True)
class Hypothesis:
    hypothesis_id: str
    title: str
    statement: str
    status: FindingStatus
    pointers: tuple[str, ...]
    priority: int  # 1 (highest) .. 5
    explanation: str | None = None


@dataclass(frozen=True)
class Remedy:
    template_id: str
    title: str
    rationale: str
    tradeoffs: tuple[str, ...]
    verification: str
    score: float  # rubric output 0..1, not a predicted gain
    pointers: tuple[str, ...]


@dataclass(frozen=True)
class Diagnosis:
    run_id: str
    facts: tuple[Fact, ...]
    hypotheses: tuple[Hypothesis, ...]
    alternatives: tuple[str, ...]
    remedies: tuple[Remedy, ...]
    limitations: tuple[str, ...]
    mode: Literal["live_model", "deterministic_fallback", "fixture"] = "deterministic_fallback"
    extra: dict = field(default_factory=dict)

    def with_(self, **changes) -> "Diagnosis":
        return replace(self, **changes)
