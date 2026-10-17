"""Evidence-grounded diagnosis and remediation ranking.

Every fact carries a JSON pointer into the immutable ``RunSummary`` and every
pointer is checked to resolve before the result leaves this module (the API
re-checks with ``checked_evidence``). Hypotheses carry an explicit status; a
single run can support a hypothesis in that run but never proves a general
causal claim.

Expectations come from ``dependency_map`` (derived from the vendored upstream
source): a hard dependency should degrade its operation, a soft one should not.
``RunSummary`` does not record the injected target, so the caller passes it
when known; without it, the diagnosis reports per-operation degradation only.

Hypothesis priority (1 = highest):
  1  supported degradation of a business-critical operation, or failed recovery
  2  supported degradation of another operation
  3  unexpected degradation or insufficient evidence
  4  expectation not supported
  5  resilience confirmed (a soft dependency was tolerated)

Remediation score (0..1) = 0.5*severity + 0.3*criticality + 0.2*applicability,
where severity = max(success-rate drop, clip((p95 ratio - 1) / 4, 0, 1)).
Finding priority = 1 + round(4 * (1 - score)). Benefits stay ``predicted``
until a comparable verification run supports them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from app.contracts import AnalysisResult, EvidenceRef, Finding, OperationAggregate, RunSummary, TopologySnapshot

from .dependency_map import BUSINESS_CRITICAL_OPERATIONS, classify
from .internal import Diagnosis, Fact, Hypothesis, Remedy
from .planner import DEFAULT_CRITICALITY

DIAGNOSIS_VERSION = "diagnosis-1"


@dataclass(frozen=True)
class DiagnosisConfig:
    min_samples: int = 20
    success_drop_supported: float = 0.05  # 5 percentage points
    success_drop_negligible: float = 0.01
    p95_ratio_supported: float = 1.5
    slow_recovery_seconds: float = 60.0
    criticality: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_CRITICALITY))


# ---------------------------------------------------------------------------
# JSON pointers


def escape_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def resolve_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise KeyError("pointer must start with '/'")
    current = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not token.isdigit() or int(token) >= len(current):
                raise KeyError("pointer index out of range")
            current = current[int(token)]
        elif isinstance(current, dict):
            if token not in current:
                raise KeyError("pointer key not found")
            current = current[token]
        else:
            raise KeyError("pointer traverses a scalar")
    return current


def summary_document(summary: RunSummary) -> dict[str, Any]:
    return summary.model_dump(mode="json")


# ---------------------------------------------------------------------------


@dataclass
class _Ctx:
    summary: RunSummary
    doc: dict[str, Any]
    phases: dict[str, int]

    def ptr(self, pointer: str) -> str:
        resolve_pointer(self.doc, pointer)  # raises if the field does not exist
        return pointer

    def op(self, phase: str, op: str, metric: str) -> str:
        return self.ptr(f"/phase_summaries/{self.phases[phase]}/operations/{escape_pointer_token(op)}/{metric}")


def _facts(ctx: _Ctx) -> list[Fact]:
    facts: list[Fact] = []
    for phase, idx in ctx.phases.items():
        for op, agg in sorted(ctx.summary.phase_summaries[idx].operations.items()):
            for metric, unit in (("success_rate", "fraction"), ("p95_ms", "ms"), ("sample_count", "samples")):
                facts.append(
                    Fact(
                        fact_id=f"F-{phase}-{op}-{metric}",
                        statement=f"{phase} phase, {op}, {metric.replace('_', ' ')}",
                        value=getattr(agg, metric),
                        unit=unit,
                        pointer=ctx.op(phase, op, metric),
                    )
                )
    s = ctx.summary
    for name, unit in (("recovery_seconds", "s"), ("recovery_status", None), ("cleanup_status", None), ("stop_reason", None)):
        facts.append(Fact(f"F-run-{name}", f"run {name.replace('_', ' ')}", getattr(s, name), unit, ctx.ptr(f"/{name}")))
    return facts


def _change(b: OperationAggregate, f: OperationAggregate) -> tuple[float, float, float]:
    drop = (b.success_rate - f.success_rate) if b.success_rate is not None and f.success_rate is not None else 0.0
    ratio = (f.p95_ms / b.p95_ms) if b.p95_ms and f.p95_ms is not None else 1.0
    severity = max(drop, min(1.0, max(0.0, (ratio - 1.0) / 4.0)))
    return drop, ratio, severity


def _describe(op: str, b: OperationAggregate, f: OperationAggregate) -> str:
    return (
        f"{op}: success rate {b.success_rate} in baseline vs {f.success_rate} during the fault; "
        f"p95 {b.p95_ms} ms vs {f.p95_ms} ms ({b.sample_count} and {f.sample_count} samples)."
    )


def analyze(
    summary: RunSummary, topology: TopologySnapshot, config: DiagnosisConfig, target_id: str | None = None
) -> tuple[Diagnosis, float]:
    """Deterministic diagnosis and the maximum supported severity."""

    doc = summary_document(summary)
    phases = {ps.phase: i for i, ps in enumerate(summary.phase_summaries)}
    ctx = _Ctx(summary, doc, phases)
    facts = _facts(ctx)
    hyps: list[Hypothesis] = []
    alternatives = [
        "A single run shows co-occurrence of the fault and the measured change; it does not establish a general causal guarantee.",
    ]
    limitations = list(summary.data_quality)

    nodes = {n.target_id: n for n in topology.nodes}
    target_name = nodes[target_id].name if target_id in nodes else target_id
    if target_id is None:
        limitations.append(
            "RunSummary does not record the injected target, so degradation is reported per operation "
            "without dependency attribution."
        )
    elif target_id not in nodes:
        limitations.append("Run target is not present in the supplied topology snapshot.")

    max_severity = 0.0
    if "baseline" not in phases or "fault" not in phases:
        limitations.append("Baseline or fault phase is missing; impact hypotheses cannot be evaluated.")
    else:
        base = summary.phase_summaries[phases["baseline"]]
        fault = summary.phase_summaries[phases["fault"]]
        b_rate, f_rate = base.achieved_rate, fault.achieved_rate
        if b_rate and f_rate is not None and abs(f_rate - b_rate) / b_rate > 0.10:
            alternatives.append(
                "Achieved load differed by more than 10% between baseline and fault phases; load change is a possible confounder."
            )
        for op in sorted(set(base.operations) | set(fault.operations)):
            b, f = base.operations.get(op), fault.operations.get(op)
            if b is None or f is None or min(b.sample_count, f.sample_count) < config.min_samples:
                hyps.append(
                    Hypothesis(
                        hypothesis_id=f"H-{op}-samples",
                        title=f"Insufficient samples for {op}",
                        statement=f"Fewer than {config.min_samples} samples in a phase; no conclusion is drawn for {op}.",
                        status="insufficient_evidence",
                        pointers=tuple(ctx.op(p, op, "sample_count") for p, a in (("baseline", b), ("fault", f)) if a),
                        priority=3,
                    )
                )
                continue
            drop, ratio, severity = _change(b, f)
            degraded = drop >= config.success_drop_supported or ratio >= config.p95_ratio_supported
            unchanged = drop < config.success_drop_negligible and ratio < config.p95_ratio_supported
            ptrs = tuple(ctx.op(p, op, m) for p in ("baseline", "fault") for m in ("success_rate", "p95_ms"))
            critical = op in BUSINESS_CRITICAL_OPERATIONS
            relation = classify(op, target_name) if target_id is not None else "unknown_target"
            text = _describe(op, b, f)

            if relation == "hard":
                status = "supported_in_this_run" if degraded else "not_supported" if unchanged else "insufficient_evidence"
                if status == "supported_in_this_run":
                    max_severity = max(max_severity, severity)
                hyps.append(
                    Hypothesis(
                        f"H-{op}-hard",
                        f"{op} has no fallback when {target_name} fails",
                        f"Upstream code treats {target_name} as a hard dependency of {op}. {text}",
                        status,
                        ptrs,
                        (1 if critical else 2) if status == "supported_in_this_run" else 3 if status == "insufficient_evidence" else 4,
                    )
                )
            elif relation == "soft":
                status = "supported_in_this_run" if unchanged else "not_supported" if degraded else "insufficient_evidence"
                if status == "not_supported":
                    max_severity = max(max_severity, severity)
                hyps.append(
                    Hypothesis(
                        f"H-{op}-soft",
                        f"{op} tolerates loss of {target_name}",
                        f"Upstream code only logs failures of {target_name} for {op}. {text}",
                        status,
                        ptrs,
                        5 if status == "supported_in_this_run" else 2 if status == "not_supported" else 3,
                    )
                )
            elif degraded:
                if relation == "unknown_target":
                    max_severity = max(max_severity, severity)
                    hyps.append(
                        Hypothesis(f"H-{op}-degraded", f"{op} degraded during the fault", text, "supported_in_this_run", ptrs, 1 if critical else 2)
                    )
                else:
                    hyps.append(
                        Hypothesis(
                            f"H-{op}-unexpected",
                            f"{op} degraded outside the predicted blast radius",
                            f"{op} is not mapped to {target_name}: possible unmapped dependency, shared resource "
                            f"exhaustion or a load-generator effect. {text}",
                            "insufficient_evidence",
                            ptrs,
                            3,
                        )
                    )
                    alternatives.append(f"Degradation of {op} outside the predicted blast radius needs a targeted follow-up run.")

        if "recovery" in phases:
            rec = summary.phase_summaries[phases["recovery"]]
            for op, r in sorted(rec.operations.items()):
                b = base.operations.get(op)
                if b is None or b.success_rate is None or r.success_rate is None or r.sample_count < config.min_samples:
                    continue
                if b.success_rate - r.success_rate >= config.success_drop_supported:
                    hyps.append(
                        Hypothesis(
                            f"H-{op}-incomplete-recovery",
                            f"{op} had not recovered to baseline",
                            f"{op} success rate {r.success_rate} in the recovery window vs {b.success_rate} in baseline.",
                            "supported_in_this_run",
                            (ctx.op("baseline", op, "success_rate"), ctx.op("recovery", op, "success_rate")),
                            1,
                        )
                    )

    if summary.cleanup_status == "failed":
        limitations.append("Fault cleanup failed; STOP is latched and findings describe an unrecovered system.")
    if summary.recovery_status in ("unknown", "pending"):
        limitations.append("Functional recovery was not verified; the recovery component is unknown, not zero.")

    remedies = advise(summary, target_id, target_name, hyps, max_severity, config, ctx)
    diag = Diagnosis(
        run_id=summary.run_id,
        facts=tuple(facts),
        hypotheses=tuple(hyps),
        alternatives=tuple(alternatives),
        remedies=tuple(remedies),
        limitations=tuple(limitations),
    )
    return diag, max_severity


# ---------------------------------------------------------------------------
# Advisor


@dataclass(frozen=True)
class _Template:
    template_id: str
    catalog_ids: frozenset[str]
    trigger: str  # "dependency" | "soft_failed" | "recovery"
    title: str
    rationale: str
    tradeoffs: tuple[str, ...]


_ALL = frozenset({"EXP-00", "EXP-01", "EXP-02", "EXP-03"})

REMEDIATION_TEMPLATES: tuple[_Template, ...] = (
    _Template(
        "REM-GRACEFUL-DEGRADATION",
        _ALL,
        "dependency",
        "Add caller-side graceful degradation for {target}",
        "Callers of {target} failed the user transaction instead of serving a reduced response.",
        ("Degraded responses must be visibly labelled to users.", "Adds code paths that need their own tests."),
    ),
    _Template(
        "REM-DEADLINES-RETRY-BUDGET",
        frozenset({"EXP-00", "EXP-02", "EXP-03"}),
        "dependency",
        "Set explicit per-call deadlines and bounded, jittered retries towards {target}",
        "Unbounded waits or retries amplify a dependency fault into caller latency and errors.",
        ("Retries add load during an outage unless capped by a retry budget.", "Deadlines that are too short cause false failures."),
    ),
    _Template(
        "REM-CIRCUIT-BREAKER",
        frozenset({"EXP-02", "EXP-03"}),
        "dependency",
        "Add a circuit breaker on calls to {target} so a slow or unreachable dependency fails fast",
        "Fast failure bounds user latency while the dependency is impaired.",
        ("Requires tuning of open/half-open thresholds.", "Fails fast also during short blips."),
    ),
    _Template(
        "REM-REPLICAS-PROBES",
        frozenset({"EXP-01"}),
        "dependency",
        "Run at least two replicas of {target} with readiness probes and Pod anti-affinity",
        "A single replica turns one Pod deletion into a full outage of {target}.",
        (
            "More CPU and memory.",
            "Does not help a scale-to-zero test, which removes every replica.",
            "A PodDisruptionBudget limits voluntary evictions only; it does not prevent direct Pod deletion.",
        ),
    ),
    _Template(
        "REM-CART-STORE-HA",
        frozenset({"EXP-02"}),
        "dependency",
        "Back cart state with a failover-capable Redis deployment (Sentinel, cluster or managed) and a failover-aware client",
        "Cart operations depend on a single Redis endpoint.",
        (
            "An independently added replica without failover orchestration and client support is not high availability.",
            "Asynchronous replication can lose the most recent cart writes on failover.",
            "Increases operational complexity.",
        ),
    ),
    _Template(
        "REM-FIX-FALLBACK",
        _ALL,
        "soft_failed",
        "Repair the fallback path for {target}",
        "Upstream code is meant to tolerate {target} failures, but the operation still degraded in this run.",
        ("Requires reproducing the failure path in a test environment.",),
    ),
    _Template(
        "REM-RECOVERY-PROBES",
        _ALL,
        "recovery",
        "Review readiness/startup probes and client reconnection for {target} to shorten functional recovery",
        "Recovery was slow, failed or incomplete after the fault was removed.",
        ("Aggressive probes can flap under load.",),
    ),
)


def advise(
    summary: RunSummary,
    target_id: str | None,
    target_name: str | None,
    hyps: list[Hypothesis],
    severity: float,
    config: DiagnosisConfig,
    ctx: _Ctx,
) -> list[Remedy]:
    label = target_name or "the injected dependency"
    dependency = [h for h in hyps if h.status == "supported_in_this_run" and h.hypothesis_id.endswith(("-hard", "-degraded"))]
    soft_failed = [h for h in hyps if h.hypothesis_id.endswith("-soft") and h.status == "not_supported"]
    incomplete = [h for h in hyps if h.hypothesis_id.endswith("-incomplete-recovery")]
    slow = summary.recovery_status == "failed" or (
        summary.recovery_seconds is not None and summary.recovery_seconds > config.slow_recovery_seconds
    )
    criticality = float(config.criticality.get(target_name or "", 0.5))
    out: list[Remedy] = []
    for t in REMEDIATION_TEMPLATES:
        if t.trigger == "dependency":
            if not dependency or summary.catalog_id not in t.catalog_ids:
                continue
            pointers = tuple(p for h in dependency for p in h.pointers)
            sev = severity
        elif t.trigger == "soft_failed":
            if not soft_failed:
                continue
            pointers = tuple(p for h in soft_failed for p in h.pointers)
            sev = severity
        else:
            if not (slow or incomplete):
                continue
            pointers = (ctx.ptr("/recovery_status"),) + (
                (ctx.ptr("/recovery_seconds"),) if summary.recovery_seconds is not None else ()
            ) + tuple(p for h in incomplete for p in h.pointers)
            sev = 0.5
        applicability = 1.0 if target_id is not None else 0.5
        score = round(min(1.0, 0.5 * sev + 0.3 * criticality + 0.2 * applicability), 3)
        out.append(
            Remedy(
                template_id=t.template_id,
                title=t.title.format(target=label),
                rationale=t.rationale.format(target=label),
                tradeoffs=t.tradeoffs,
                verification=(
                    f"Re-run {summary.catalog_id}"
                    + (f" on {target_id}" if target_id else "")
                    + f" with load profile {summary.load_profile.load_profile_id} after the change and compare the same phase metrics."
                ),
                score=score,
                pointers=tuple(dict.fromkeys(pointers)),
            )
        )
    out.sort(key=lambda r: (-r.score, r.template_id))
    return out


# ---------------------------------------------------------------------------
# Conversion to the shared contract


def to_contract(diag: Diagnosis, summary: RunSummary) -> AnalysisResult:
    def refs(pointers) -> list[EvidenceRef]:
        return [EvidenceRef(run_id=summary.run_id, evidence_hash=summary.evidence_hash, pointer=p) for p in pointers]

    hypotheses = [
        Finding(
            title=h.title,
            explanation=h.explanation or h.statement,
            status=h.status,
            priority=h.priority,
            evidence_refs=refs(h.pointers),
        )
        for h in sorted(diag.hypotheses, key=lambda h: (h.priority, h.hypothesis_id))
    ]
    recommendations = [
        Finding(
            title=r.title,
            explanation=f"{r.rationale} Trade-offs: {' '.join(r.tradeoffs)} Verify: {r.verification} "
            f"Rubric score {r.score:.2f}; no numeric benefit is claimed.",
            status="predicted",
            priority=max(1, min(5, 1 + round(4 * (1 - r.score)))),
            evidence_refs=refs(r.pointers),
        )
        for r in diag.remedies
    ]
    return AnalysisResult(
        source=summary.source,
        run_id=summary.run_id,
        mode=diag.mode,
        measured_facts=[f.render() for f in diag.facts],
        hypotheses=hypotheses,
        alternatives=list(diag.alternatives),
        recommendations=recommendations,
        evidence_refs=refs(dict.fromkeys(f.pointer for f in diag.facts)),
        limitations=list(diag.limitations),
    )
