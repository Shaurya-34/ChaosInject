import hashlib
import html
import json
from app.contracts import RunSummary, AnalysisResult
from .errors import PortError


def digest(value) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def summary_hash(summary: RunSummary) -> str:
    return digest(summary.model_dump(mode="json", exclude={"evidence_hash"}))


def checked_evidence(summary: RunSummary, analysis: AnalysisResult):
    if summary_hash(summary) != summary.evidence_hash or summary.run_id != analysis.run_id or summary.source != analysis.source:
        raise PortError("INVALID_EVIDENCE", 502)
    root = summary.model_dump(mode="json")
    refs = analysis.evidence_refs + [r for f in analysis.hypotheses + analysis.recommendations for r in f.evidence_refs]
    for ref in refs:
        if ref.run_id != summary.run_id or ref.evidence_hash != summary.evidence_hash or not ref.pointer.startswith("/"):
            raise PortError("INVALID_EVIDENCE", 502)
        value = root
        try:
            for piece in ref.pointer[1:].split("/"):
                key = piece.replace("~1", "/").replace("~0", "~")
                value = value[int(key)] if isinstance(value, list) else value[key]
        except (KeyError, IndexError, ValueError, TypeError):
            raise PortError("INVALID_EVIDENCE", 502) from None


def report(summary: RunSummary, analysis: AnalysisResult) -> str:
    checked_evidence(summary, analysis)
    esc = lambda x: html.escape(str(x), quote=True)
    sections = []
    for phase in summary.phase_summaries:
        for operation, metrics in phase.operations.items():
            sections.append(f"<tr><td>{esc(phase.phase)}</td><td>{esc(operation)}</td><td>{metrics.attempted}</td><td>{metrics.succeeded}</td><td>{metrics.failed}</td><td>{esc(metrics.p95_ms if metrics.p95_ms is not None else 'missing')}</td><td>{esc(phase.started_at.isoformat())} — {esc(phase.ended_at.isoformat())}</td></tr>")
    findings = "".join(f"<li><strong>{esc(f.title)}</strong> ({esc(f.status)}, priority {f.priority}) — {esc(f.explanation)}</li>" for f in analysis.hypotheses + analysis.recommendations)
    refs = "".join(f"<li>{esc(r.pointer)} · {esc(r.evidence_hash)}</li>" for r in analysis.evidence_refs)
    return f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Resilience findings</title><style>body{{font:16px system-ui;max-width:1000px;margin:40px auto;padding:24px;color:#162126}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccc;padding:8px;text-align:left}}.label{{background:#fff1b8;padding:12px}}code{{overflow-wrap:anywhere}}</style><h1>Bosch Resilience AI · Findings</h1><p class="label">Data source: {esc(summary.source)}. Fixture values are synthetic and are not live measurements.</p><p>Run: {esc(summary.run_id)} · sandbox: {esc(summary.sandbox_id)} · application revision: {esc(summary.application_revision or 'unverified')}</p><p>Fault: {esc(summary.catalog_id)} · topology hash: <code>{esc(summary.topology_hash)}</code></p><p>Load: {esc(summary.load_profile.model_dump_json())}</p><table><tr><th>Phase</th><th>Operation</th><th>Attempts</th><th>Success</th><th>Failed</th><th>p95 ms</th><th>Window UTC</th></tr>{''.join(sections)}</table><p>STOP: {esc(summary.stop_reason)} · Cleanup: {esc(summary.cleanup_status)} · Recovery: {esc(summary.recovery_status)} · Recovery seconds: {esc(summary.recovery_seconds)}</p><h2>Ranked findings and recommendations</h2><ol>{findings}</ol><h2>Resilience score</h2><pre>{esc(summary.score.model_dump_json(indent=2))}</pre><h2>Evidence</h2><code>{esc(summary.evidence_hash)}</code><ul>{refs}</ul><h2>Limits</h2><ul>{''.join('<li>'+esc(x)+'</li>' for x in summary.data_quality + analysis.limitations)}</ul></html>"""
