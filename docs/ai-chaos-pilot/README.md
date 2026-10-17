# Bosch Resilience AI

Integrated team prototype: Sachin's API/dashboard, Tooshita's persistent STOP guard, Navya's telemetry/platform and independent verifier, and Gautam's deterministic intelligence and bounded River learner. The runnable local demo prevents an unsafe injection; cluster faults and SQL execution remain disabled.

## Immediate demo

1. Start the separate target and authenticated API/dashboard.
2. Connect with the temporary local operator token printed by the launcher.
3. Enter an **Outage duration (seconds)** from 1 to 600, then click **Check duration**.
4. A healthy target with a duration of 1–60 seconds returns `ALLOW` and a green **Safety check passed** popup. Success means validation passed; no outage is executed.
5. A duration above 60 seconds returns `BLOCK`, latches STOP and opens **Error has occurred** / **Injection blocked before execution**. Reset after the health check to try again. Every result dispatches **zero** target mutations.

The local target exposes health and an independent mutation witness. This demonstration intentionally has no ability to execute faults. It verifies prevention; full Kubernetes execution and cleanup are still pending. SQL analysis uses the real AST guard with an empty allowlist until a lab schema is enrolled. The River learner is connected but cold; no SQL executions or trained predictions are claimed. No external LLM is configured.

## Setup (Windows PowerShell)

Tested with Python 3.12.14 and Node 22.14.0. Python 3.11+ and Node 22 are the development targets. Install from official vendor distributions if not available.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
Set-Location frontend
npm.cmd ci
npm.cmd run build
Set-Location ..
powershell -ExecutionPolicy Bypass -File scripts\start-demo.ps1
```

Open **http://127.0.0.1:8000**. Paste the temporary token from the launcher. The token is held in RAM, not browser storage. Leave the launcher's terminal running; Ctrl+C stops its two processes. The API uses port 8000 and the read-only target uses 8090. Do not run two launchers simultaneously. If either port is occupied, stop the older demo before launching.

On Linux/macOS use `.venv/bin/python`, `npm ci --prefix frontend`, `npm run build --prefix frontend`, then `sh scripts/start-demo.sh`. Windows is the environment actually tested here; other platforms require verification.

## Development and fixture mode

Fixture mode supplies synthetic contracts, an illustrative partial topology, a completed example run and report. Mutation routes are disabled by default. For a clearly fake lifecycle demonstration use `scripts/start-demo.ps1 -FakeFixtures`. No fixture numbers are reported as live measurements, and there is no fabricated resilience score.

For Vite development, run the API on 8000 and `npm run dev` in `frontend/` on 5173. The proxy forwards `/api`. CORS only permits exact loopback origins; Bearer authentication is required on API/docs/schema routes. Public UI assets contain no operator token.

## Generate contracts and run checks

```powershell
.\.venv\Scripts\python.exe scripts\export_contracts.py
npm.cmd run types --prefix frontend
.\.venv\Scripts\python.exe scripts\export_contracts.py --check
.\.venv\Scripts\python.exe -m pytest -q
npm.cmd run build --prefix frontend
```

`backend/app/contracts/models.py` is authoritative. Generated schemas/OpenAPI are in `contracts/v1/`; generated TypeScript is `frontend/src/generated.ts`. CI regenerates and rejects drift. Regenerate fixtures deliberately with `scripts/make_fixtures.py`; their timestamps and example values are fixed and labelled synthetic.

Browser tests: install the Chromium test browser with `npx playwright install chromium` in `frontend/`; run `npm test` there. Set `PLAYWRIGHT_BROWSERS_PATH` to a writable directory if needed. Tests start their own local API and target with a disposable test token. A genuine local HTTP/browser test is distinct from mocked port tests. See `docs/integration/TESTING.md` for the results from this session.

## Integrate the four owners

| Owner | Implementation |
|---|---|
| Ananya | Platform discovery, enrolled target identity, pinned Online Boutique, Kubernetes deployment and verified Redis proxy route |
| Tooshita | Injection, exact approvals, job ownership, concurrency, watchdog, STOP, fault journal, cleanup, bounded SQL execution |
| Navya | Independent injection-effect/recovery tests, probes, baseline/fault/recovery summaries, plan estimates and deterministic scoring |
| Gautam | AST/SQL policy, planning, bounded learner, evidence-grounded reasoning |
| Sachin | Shared types, composition, authentication, three dashboard views, popup, contract/API/UI integration tests and packaging |

Implement the protocols in `backend/app/api/ports.py` and return a `Ports` object from an importable factory. Configure `DATA_MODE=live`, `LIVE_PORTS_FACTORY=your_module:create_ports`, `OPERATOR_TOKEN`, `SANDBOX_ID`, `KUBE_CONTEXT`. The factory must explicitly attest `runtime_safety_ready=True` only after the independent worker/watchdog and reconciliation path have been verified. Missing modules prevent startup. Live mode never falls back to fixtures. Source mismatches are refused.

SQL execution stays disabled unless `sql_execution_ready=True` in a live adapter. The owner must finish AST validation, intent expiry, current-state revalidation, actor-bound single-use tokens, read-only restricted credentials and bounded execution before enabling it. The API receives only a token at execution, never replacement SQL. Query inputs are transient, errors are sanitized and the editor clears after analysis/expiry.

Replay mode requires `REPLAY_BUNDLE` matching `ReplayBundle.schema.json`: explicit replay source, recorded-from-live declaration and valid summary hashes/pointers. Replay is read-only. A hash proves consistency, not authenticity; obtain the bundle from a trusted recorder. No authentic recording exists in this initial workstream.

## Safety and scope

- One local operator token, Bearer header only, no cookies or credentials in URLs. Strict browser origins and bounded request bodies.
- STOP receipts acknowledge a request; they never imply verified recovery. Reset requires successful reconciliation and functional recovery.
- API logs omit access requests and unexpected errors have fixed sanitized bodies; never enable raw SQL/body logging. No analytics, browser storage, request dataset, hosted model or live keys.
- No SQL parser, learner, metric scoring or cluster mutation is reimplemented in API/UI. The isolated local demo guard is not the team live runtime.
- The independent live watchdog, journal/restart reconciliation and host/cluster recovery behavior remain owner integration gates. No live cluster exists here.
- Adaptive action routes are disabled. No SQL execution or committed-write rollback is claimed.

## Team handoff and publishing

Updated full prompts are under `prompts/`; `scripts/update_prompt_pack.py` regenerates the downloadable pack. Ownership is a proposed team division from the supplied pack, not evidence that messages were sent or teammate work is finished.

Team code is Apache-2.0. No upstream application code is bundled; preserve its license/notices when Ananya pins it. The shared repository is https://github.com/ssaaach/AI-Chaos-Pilot. Source publication uses the authenticated GitHub browser; the connected integration has read access but rejects writes. No CLI credential files were read. `scripts/publish.ps1` supports later pushes to a feature branch in this existing repository after normal CLI authentication; it never creates a second repository or force-pushes. Teammate review and main protection remain team setup tasks. See the repository's Actions tab for CI status; local checks are recorded separately in `docs/integration/TESTING.md`.

The architecture sheet, real local demo test evidence, fixture findings report, team form and demo recording instructions are under `docs/integration/` / `reports/`. Actual event submission still requires authentic Kubernetes findings, the three required fault mechanisms and a final team-reviewed recording.

## Independent prevention acceptance

Run `.venv/Scripts/python.exe scripts/telemetry/run_prevention_acceptance.py` (Windows) or `.venv/bin/python scripts/telemetry/run_prevention_acceptance.py`. This calls the real runtime guard against a separate HTTP target and verifies zero executor invocations, zero target mutations, healthy before/after and STOP. Evidence is written to `reports/local_demo_prevention.json`; no resilience score is assigned to a prevented attempt.

## Day 1 demo scope

One deterministic computation compares the user-entered duration with 60 seconds: `duration <= 60` passes validation, while `duration > 60` blocks before dispatch. STOP and unavailable or stale health always override a passing duration. STOP persists across API restarts; the dashboard reads the authoritative latch and displays the error popup. Navya independently corroborates zero executor calls and zero target mutations over real HTTP. This is prevention evidence, not an executed outage or a resilience score.

Merged team work: Navya platform/telemetry (already on main), Gautam intelligence, and Tooshita runtime (the supplied ZIP matches her branch; archive caches, Git metadata and a stray duplicate package were excluded). Original authorship is preserved in merge history.

Local verification: 329 backend tests, 4 Playwright browser tests, frontend build, contract export check and real-HTTP acceptance passed. Cluster deployment, full chaos execution, SQL execution and external LLM calls are deferred.

If an older demo occupies ports 8000/8090, use `powershell -ExecutionPolicy Bypass -File scripts/start-demo.ps1 -ApiPort 8100 -TargetPort 8190`, then open `http://127.0.0.1:8100`.
