# Executed validation — 10 October 2026

## Completed here

- Python contract/API/integration suite: **32 passed**. Covers fixture schema validation, nonfinite/extra-field rejection, missing reasons, changed evidence/hash/pointers, authentication, hostile origins, body limit, safe errors, exact approvals, expiry, consumed approvals, idempotent start, pending STOP, reset/reconciliation, model-independent STOP, report values, SQL non-echo, live-mode refusal, source mismatch, replay read-only/tamper rejection and local guard rules.
- TypeScript strict compilation and Vite production build: passed. There is an informational bundle-size warning for the chart library.
- Schema/OpenAPI generation drift check: passed. TypeScript regenerated from exported OpenAPI.
- Playwright real-service tests: **3 passed**. A separate running HTTP target remains healthy with zero received mutations after the guard rejects the unsafe duration. The popup appears and can be acknowledged. Reloading the dashboard preserves the server's STOP indicator; a verified healthy reset enables a new prevention attempt. SQL input clears without echoing candidate text. Mobile layout keeps STOP and the injection button visible without horizontal overflow.
- Python dependency audit and npm audit after fixes: **no known vulnerabilities reported** at check time. This does not guarantee the absence of unknown vulnerabilities.

Python 3.12.14, Node 22.14.0, FastAPI 0.143.0, Starlette 1.7.0, pytest 9.1.1, Vite 6.4.4 and Playwright 1.64.0 were actually installed/tested. The complete Python/Node dependency versions are in the locks. Starlette emits a test-client migration warning about future HTTPX2 use; the tests currently pass with pinned HTTPX.

## Evidence interpretation

The mock port suite tests API composition, not Kubernetes effects. The separate HTTP/browser test is real LOCAL DEMO evidence. The demo has no mutation executor and therefore makes no claim about recovering an injected outage. It proves rejection before dispatch for the specific demo path and continued target health. The fixed fixtures illustrate report/layout contracts only.

The exported local demo evidence records a before/after health response, the actual guard decision, the request ID, fixed cap, requested duration and zero mutations. Raw SQL, parameter values, credentials, result rows and learner samples are excluded. Browser recording is a local demo walkthrough, not authentic cluster experiment footage.

## Unverified / unavailable

- Docker, kubectl, kind, enrolled sandbox identity and pinned Online Boutique are unavailable here.
- Team live Platform/Runtime/Telemetry/Intelligence implementations are not provided.
- Actual chaos effects, threshold-triggered cleanup, independent live watchdog survival, journal crash/restart reconciliation, SQL approval/cancel/caps, incremental learning, full percentile collection and deterministic resilience scoring remain unverified owner work.
- Source publication uses the existing public repository https://github.com/ssaaach/AI-Chaos-Pilot through the authenticated browser. Branch protection, teammate review and GitHub CI execution are separate gates; check Actions for the current run. A local runnable bundle is also supplied. The Windows launcher cleanup path could not be fully verified under this environment's process permissions; the two read-only local services remain running for the demo.

Use the revised prompt pack to assign those later gates. Do not mark the event's official live Day 1/Day 2 criteria complete from these local results.
