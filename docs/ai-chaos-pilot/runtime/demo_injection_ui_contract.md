# Local demo injection — UI response contract

Integration/UI owns the injection button and accessible error popup. Runtime returns structured evidence only; do not infer target failure or recovery success from a blocked request.

## Endpoint

`POST /api/v1/demo/injection` (requires `DATA_MODE=demo` and Bearer operator token)

Request body:

```json
{
  "fault_type": "service_pause",
  "target_id": "demo-target",
  "duration_seconds": 90
}
```

## User-defined duration

The input accepts whole seconds from 1 to 600. The backend independently validates the type and range. With fresh healthy target data and no STOP latch, 1–60 seconds returns `ALLOW`, `rule_id=DURATION_WITHIN_LIMIT`, `stop_latched=false`, heading `Safety check passed` and body `Duration accepted for demo`. This is validation success only: `executed=false` and `mutations_dispatched=0` always.

Above 60 seconds returns `BLOCK` and latches STOP. A prior STOP or unavailable/stale health also returns `BLOCK`, even for a duration within the limit. Invalid inputs return HTTP 422; the frontend prevents submission and shows an input message.

## Error popup copy (exact)

| Role | Text |
|------|------|
| Heading | `Error has occurred` |
| Body | `Injection blocked before execution` |

Use `popup_heading` and `popup_body` from the JSON response. Do not rewrite or merge into a single line in accessibility labels unless both strings remain verbatim.

## Details to show (never imply crash or recovery)

Display from the response:

- `rule_id` (for example `DURATION_HARD_LIMIT`)
- `target_status` (`healthy` or `unavailable`)
- `executed` (always `false` for this demo path)
- `mutations_dispatched` (always `0`)
- `duration_seconds` and `maximum_seconds` (60)
- `reason` (human-readable rejection; describes the request, not target crash)
- `evidence_label`: always `LOCAL DEMO`

Do **not** show recovery-success messaging, outage confirmation, or resilience scores for this flow. The target did not execute a fault.

## Example blocked response

```json
{
  "source": "live",
  "schema_version": "1.0",
  "request_id": "...",
  "decision": "BLOCK",
  "evidence_label": "LOCAL DEMO",
  "popup_heading": "Error has occurred",
  "popup_body": "Injection blocked before execution",
  "message": "Error has occurred — Injection blocked before execution.",
  "rule_id": "DURATION_HARD_LIMIT",
  "executed": false,
  "mutations_dispatched": 0,
  "stop_latched": true,
  "target_status": "healthy",
  "duration_seconds": 90,
  "maximum_seconds": 60
}
```

Reset uses existing `POST /api/v1/safety/reset` with the same Bearer token conventions after fresh target health verification.
