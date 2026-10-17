# Day 2 final demo

The goal is to show the full loop:
1. The AI proposes experiments.
2. A human runs one from the dashboard.
3. The controller injects a real fault, stops itself when a safety rule trips, restores the fault and verifies recovery.
4. The monitoring agent explains the weaknesses, with evidence.

It takes about 8 minutes. Run every command from the repo root in PowerShell on an **amd64** machine. Network faults aren't observable on Docker Desktop for Mac. On macOS/Linux, use `python3` and `.venv/bin/python`.

How the system fits together: [architecture.md](architecture.md).

## One-time setup per machine

```powershell
python sandbox/hotel/prepare.py
cd sandbox/hotel/.runtime; docker compose pull --ignore-buildable; cd ../../..
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
cd frontend; npm ci; npm run build; cd ..
```

## Pre-flight (15 min before)

```powershell
# 1. App
cd sandbox/hotel/.runtime; docker compose up -d --build; cd ../../..
python sandbox/hotel/smoke.py                       # all 7 must PASS

# 2. Observability (joins the hotel network, so start it after the app)
docker compose -f sandbox/observability/compose.yaml up -d
# http://localhost:9090/targets -> chaos-controller, blackbox-hotels, cadvisor all "up"

# 3. Nothing left over from earlier runs
python -m chaos.controller --recover                # must say "nothing to recover"
docker ps -a --filter name=pumba --format "{{.Names}}"   # must print nothing

# 4. API + dashboard, with execution enabled (keep this terminal open)
$env:OPERATOR_TOKEN = (python -c "import secrets; print(secrets.token_urlsafe(32))"); $env:OPERATOR_TOKEN
$env:CHAOS_EXECUTION_ENABLED = "true"
.\.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --app-dir backend --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000, paste the token printed above, and open **Chaos runs**. Also open these tabs:
- [architecture.md](architecture.md) on GitHub
- http://localhost:9090
- http://localhost:5000

**Rehearse once.** The findings and proposals come from the runs saved **on this machine** (`results/` isn't in git). Run each experiment below once during pre-flight so the panels have data, then restart `rate` (see step 4).

Use a large font. Don't pipe controller output into `Select-Object` or `head`; it kills the run mid-fault.

## Run order

| # | Time | Show | Say |
|---|---|---|---|
| 1 | 0:45 | Architecture diagram | Only the controller can change the system. The AI proposes and explains; it never runs anything |
| 2 | 0:45 | **Suggested next experiments** panel | The injection agent ranks experiments from the dependency map and run history. Every proposal passes the policy check, and candidates the policy refuses are listed with the reason |
| 3 | 1:30 | Run `hotel_geo_stop` | A hard dependency fails, and the system stops itself |
| 4 | 1:30 | Run `hotel_memcached_rate_stop` | The finding: a cache outage crashes `rate` |
| 5 | 1:15 | Run `hotel_rate_loss` + Prometheus | 30% packet loss on one backend becomes a 30× slowdown at the edge |
| 6 | 0:45 | Stop button | The human kill switch |
| 7 | 1:15 | **Findings** panel + report | Ranked weaknesses with run IDs and fixes |
| 8 | 0:15 | Wrap-up | 3 fault types, safety-first, AI advisory |

### 3. Hard dependency: `hotel_geo_stop`

Click **Run** on `hotel_geo_stop`. The live panel shows the baseline, then the fault.

**Expect:**
- 5 failed probes, then `SAFETY STOP: 5 consecutive failed probes`
- restored, recovered in about 1 s
- the run opens in the detail chart, with the fault phase shaded and the failed probes marked

**Say:** it touched only an allowlisted container, checked health first, stopped after 5 failures instead of running the full 20 s, and *verified* recovery.

### 4. The finding: `hotel_memcached_rate_stop`

Click **Run**.

**Expect:**
- a safety stop, restored
- recovery probes keep failing for 30 s
- the runs table shows the row in red (**not recovered**)

**Say:**
- Stopping a cache should only make things slower. Instead the `rate` service **crashed**: it calls `log.Panic` on any cache error (`services/rate/server.go:114`).
- Restoring the cache didn't bring the app back.
- The controller caught it because it verifies recovery instead of assuming it.

Then restore `rate` (this is **required** before the next run):

```powershell
cd sandbox/hotel/.runtime; docker compose start rate; cd ../../..
```

If you skip this, the next run is refused with `baseline already unhealthy`. That's the pre-flight safety check working.

### 5. Latency amplification: `hotel_rate_loss`

Click **Run**.

**Expect:**
- `/hotels` goes from about 20–50 ms to about 1.5 s, with timeouts
- a safety stop (a p95 latency or consecutive-failure rule)
- recovers instantly once the loss is removed

**Show** the independent view in Prometheus at http://localhost:9090/query?g0.expr=probe_duration_seconds&g0.range_input=5m. The blackbox exporter, probing from *inside* the hotel network, sees the same spike. Once T4 is merged, the same charts appear under the run detail.

**Say:** modest loss on one backend becomes TCP retransmission waits, which surface as a 30× slowdown at the user-facing edge.

### 6. Kill switch

Click **Run** on `hotel_geo_stop`, and press **Stop** as soon as the live panel shows the fault phase.

**Expect:** `stop reason: kill switch (operator stop)`, restored, recovered.

**Say:** Stop writes a file that the controller checks on every probe. The controller runs as its own process, so even if the API crashes the fault is still restored. We tested this by killing the API mid-fault.

### 7. Findings

Open the **Findings** panel.

**Expect** these, ranked by severity:
- `memcached-rate` stop: **Not recovered / crashed**
- `geo` stop and `rate` packet loss: **full outage of a critical path**

Each finding cites its run IDs and gives a suggested fix. Click **Download HTML** for the findings report.

The resilience score appears here once T6 is merged.

## Claims: say / don't say

| ✅ Say | ❌ Don't say |
|---|---|
| "The controller enforces safety; the AI only proposes and explains" | "The AI runs the experiments" |
| "We crashed `rate` by stopping its cache" | "We crashed 4 services" (the other 3 are confirmed in the code, not tested) |
| "The bug is in upstream DeathStarBench" | "We found a bug in our code" |
| "Prometheus is an independent view" | "Prometheus decides when to stop" (the controller's own probes do) |
| "3 fault mechanisms: container stop, network delay, packet loss" | "We tested every service" |

## If something goes wrong

| Problem | Fix |
|---|---|
| `refused: baseline already unhealthy` | Usually `rate` is still down from step 4: `cd sandbox/hotel/.runtime; docker compose start rate` |
| Run button greyed out / `execution disabled` | The API was started without `CHAOS_EXECUTION_ENABLED=true`. Restart it with step 4 of pre-flight |
| `409 a run is already active` | Wait for the live panel to finish, or press Stop |
| Leftover-fault banner | Click **Recover**, or run `python -m chaos.controller --recover` |
| Dashboard won't load | Run the same experiment from the CLI: `python -m chaos.controller experiments/hotel_geo_stop.json` |
| Prometheus targets down | `docker compose -f sandbox/observability/compose.yaml up -d`. Prometheus is optional; runs work without it |
| Hotel stack won't start | Fallback: `docker compose -f sandbox/dummy/docker-compose.yml up -d`, then `dummy_web_stop` |

## Shutdown

```powershell
# Ctrl+C in the API terminal
docker compose -f sandbox/observability/compose.yaml stop
cd sandbox/hotel/.runtime; docker compose stop; cd ../../..
```
