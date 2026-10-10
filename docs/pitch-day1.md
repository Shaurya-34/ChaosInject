# Day 1 checkpoint pitch

About 6–7 minutes, then Q&A. Each person explains what they built. One **driver** sits at the demo machine and runs every command; any of us can drive (currently Ayush). Speakers talk, the driver types.

**Commands:** Windows PowerShell as written. On macOS/Linux use `python3`.
**Demo runbook** (exact steps and expected output): [demo-day1.md](demo-day1.md)

## Run order

| # | Speaker | Time | Topic | On screen |
|---|---|---|---|---|
| 1 | Shaurya | 1:00 | Problem, approach, safety principle | README architecture |
| 2 | Ayush | 1:00 | The sandbox | `docker ps`, smoke test |
| 3 | Krishna | 1:00 | Dependency map + baseline | Diagram, baseline numbers |
| 4 | Parikh | 1:00 | Safety controller + results | Policy, `hooks list` |
| 5 | Shaurya | 1:30 | Live faults + the finding | geo stop, memcached-rate stop |
| 6 | Inani | 0:45 | Chaos tool + network delay | #4 evaluation (live delay only if #28 merged) |
| 7 | Shaurya | 0:30 | Day 2 plan | Board, #11, #13 |

## Pre-flight (driver, 10 min before)

First time on a machine: `python sandbox/hotel/prepare.py`, then `docker compose pull --ignore-buildable` in `sandbox/hotel/.runtime` (see README Setup).

```powershell
cd sandbox/hotel/.runtime; docker compose up -d --build; cd ../../..
python sandbox/hotel/smoke.py                # all 7 must PASS
python -m chaos.controller --recover         # should say "nothing to recover"
```

Open tabs: http://localhost:5000, http://localhost:16686, [README](../README.md), [dependency-map.mmd](dependency-map.mmd) on GitHub (renders the diagram), [baseline.md](baseline.md), [policy.json](../chaos/policy.json).
Use a large terminal font. Never pipe controller output into `head` / `Select-Object` (it kills the run mid-fault).

---

## 1. Shaurya: problem and approach (1:00)

**Say:**
- Microservices fail in ways nobody predicts. Chaos engineering breaks things on purpose, in a controlled way, to find weaknesses before users do.
- We built a controller that injects one bounded fault into a real 24-container app, measures the impact, stops automatically when safety thresholds are breached, restores the fault and verifies recovery.
- **Core principle: the controller enforces safety, not the AI.** On Day 2 the AI agents recommend experiments and explain findings; they can never run commands or change limits.

**Show:** the README architecture diagram and component table.

**Handoff:** "Ayush built the sandbox we break."

## 2. Ayush: the sandbox (1:00)

**Say:**
- DeathStarBench Hotel Reservation (Apache-2.0), a research microservice benchmark: 10 Go gRPC services, 8 MongoDB, 4 memcached, Consul, Jaeger.
- Reproducible: every image is pinned by digest; one script prepares it from the unmodified upstream Compose file.
- **Restart is deliberately off**, so Docker can't hide a failure before our controller sees it.
- We fixed real setup issues: a config mount at the wrong path, Windows checkout failures, and readiness gates.
- From inspecting the code: `review` and `attractions` are mis-wired for service discovery in the shipped config, and Consul registrations have no health checks.

**Driver runs:**
```powershell
docker ps --filter name=hotel-sandbox --format "{{.Names}}  {{.Status}}"
python sandbox/hotel/smoke.py
```
**Expect:** 24 containers; `PASS` ×7 (login, search, recommendation, a reservation verified in MongoDB, Jaeger).

**Evidence:** PR #30, [hotel-reservation-dependencies.md](hotel-reservation-dependencies.md)

**Handoff:** "Krishna mapped how these services depend on each other."

## 3. Krishna: dependency map and baseline (1:00)

**Say:**
- The map is data, not just a picture: 24 nodes, 41 edges, the call order per endpoint, and broken edges marked. The controller and the Day 2 agents read it.
- `/hotels` = frontend → search → geo, rate → reservation → profile, so one request exercises 5 services.
- Baseline (wrk2, 50 req/s, 60 s, mixed workload): **p50 4.6 ms, p99 17.4 ms, 0 errors** on amd64.
- Debugging story: the first baseline showed 56 timeouts. That wasn't the app. The Lua script needed `luasocket` and silently fell back to `GET /`, and 10 connections at 5 req/s idled past wrk2's 2 s socket timeout. Same result on two machines; fixed both.

**Show:** the rendered `dependency-map.mmd`; the results table in `baseline.md`.

**Evidence:** #5, #6, PRs #32, #35, #36

**Handoff:** "Before we break anything, Parikh's safety layer decides when to stop."

## 4. Parikh: safety controller and results (1:00)

**Say:**
- Three stop rules are checked after every probe: **5 consecutive failures** (full outage), **error rate > 50%** over the last 10 probes, **p95 latency** above min(2000 ms, max(5× baseline p95, 200 ms)), with at least 3 slow probes so one spike can't stop a run.
- **Fail-safe:** a malformed sample counts as a failure, and an error inside the safety check stops the run.
- **Pre-flight:** the controller refuses to inject if the baseline shows the app is already unhealthy.
- Experiments can't change the policy. Every run is saved with all samples, the stop reason, recovery time and a summary.

**Show:** `chaos/policy.json`. **Driver runs:**
```powershell
python -m chaos.hooks list
```
**Expect:** a table of runs saved **on this machine** (error %, p95, recovery seconds, stop reason). Results aren't in git, so a fresh machine shows nothing: then show this command again at the end of section 5.

**Evidence:** #8, #9, PRs #29, #31, [SAFETY.md](SAFETY.md)

**Handoff:** "Now let's break it."

## 5. Shaurya: live faults and the finding (1:30)

**Driver runs** (hard dependency):
```powershell
python -m chaos.controller experiments/hotel_geo_stop.json
```
**Expect:** 5 `[baseline] ok 200` lines, then `[fault] FAIL status=500` ×5, then `SAFETY STOP: 5 consecutive failed probes`, `restored`, `recovered: True in ~1s`.

**Say:** the controller only touched an allowlisted container, checked health first, stopped after 5 failures instead of the full 20 s, and *verified* recovery.

**Driver runs** (the finding):
```powershell
python -m chaos.controller experiments/hotel_memcached_rate_stop.json
docker ps -a --filter name=hotel-sandbox-rate-1 --format "{{.Names}}  {{.Status}}"
docker logs hotel-sandbox-rate-1 2>&1 | Select-String "panic:" | Select-Object -First 1
```
(macOS: `docker logs hotel-sandbox-rate-1 2>&1 | grep -m1 "panic:"`)

**Expect:** a safety stop, then `[recovery] FAIL` for 30 s, `recovered: False`; `rate` shows `Exited`; `panic: Memmcached error ... EOF`.

**Say:**
- A cache outage should only make things slower. Instead the `rate` **service crashed**: it calls `log.Panic` on any cache error (`services/rate/server.go:114`). Restoring the cache didn't bring the app back; it needs a manual restart.
- The same pattern appears in the code of 3 more services (profile, reservation, review).
- Our controller caught it because it verifies recovery instead of assuming it.

**Driver recovers:**
```powershell
cd sandbox/hotel/.runtime; docker compose start rate; cd ../../..
```

**Handoff:** "Inani picked the tool for the next fault types."

## 6. Inani: chaos tool and network delay (0:45)

**Say:**
- We evaluated Pumba against ChaosLabs (fraware/chaoslabs). ChaosLabs brings its own controller, agents and dashboard, duplicating what we built. Pumba is a single Docker-native tool driven over the Docker socket, so it slots into our controller.
- The network-delay fault uses Pumba's netem with a separate `tc` image. It verifies the delay is *active* before measuring and *gone* after restoring, and raises an error if not.
- Measured in a manual Pumba test: latency 32 ms → 278 ms during the delay → 20.6 ms after.

**Show:** the #4 evaluation comment.
**Only if PR #28 has merged:** the driver runs `python -m chaos.controller experiments/dummy_web_delay.json` (needs `docker compose -f sandbox/dummy/docker-compose.yml up -d`). Expect `[fault]` lines about 200 ms slower and a `SAFETY STOP: p95 latency`. If #28 isn't merged, describe it as in progress. **Don't run it.**

**Handoff:** "Shaurya on what's next."

## 7. Shaurya: Day 2 (0:30)

**Say:**
- **Injection agent (#11):** reads the dependency map and past results and proposes the next experiment with a rationale. The controller validates it against policy.
- **Monitoring agent (#13):** reads results, ranks weaknesses by severity and suggests fixes, citing run IDs.
- **Fix and re-run:** treat cache errors as cache misses in `rate`, re-run the same experiment, and show graceful degradation instead of a crash. A before/after.
- At least 3 fault types, the findings report, a resilience score.

**Show:** the project board.

---

## Claims: say / don't say

| ✅ Say | ❌ Don't say |
|---|---|
| "We crashed `rate` by stopping its cache" | "We crashed 4 services" (the other 3 are code-confirmed, not tested) |
| "The same pattern appears in 3 more services' code" | "The whole app is fragile" |
| "Network delay is implemented and in review" (unless #28 merged) | "We ran network delay on the hotel app" (not yet) |
| "The bug is in upstream DeathStarBench" | "We found a bug in our code" |
| "Day 2: AI agents recommend; the controller enforces" | "The AI runs the experiments" |

## Q&A prep

| Question | Who | Answer |
|---|---|---|
| Isn't the crash only because restart is off? | Shaurya | No. Restart hides it but doesn't fix it: with the cache still down, `rate` restarts and crashes on the next request, so it loops while `/hotels` keeps failing. Restart is off so failures are visible |
| Is that your bug? | Shaurya | No, it's in DeathStarBench's own code. Our tool surfaced it automatically |
| What's the fix? | Shaurya | Treat cache errors as a cache miss and fall back to MongoDB. That's our Day 2 before/after |
| What stops a fault running forever? | Parikh | The experiment duration (≤ 60 s), a 120 s hard timeout, Ctrl+C, and the safety rules. If the controller process is killed, a marker file makes the next run (or `--recover`) restore the fault |
| How do you stop it touching the wrong thing? | Parikh | An allowlist of targets and fault types in `policy.json`; experiments can't change it; only our local sandbox |
| Why not Kubernetes / Chaos Mesh? | Inani | It's Kubernetes-only and heavy for a laptop sandbox. Docker Compose plus Pumba covers our faults |
| How do you know the baseline is valid? | Krishna | Fixed-rate wrk2 with the upstream workload, 0 errors, and the bogus first run explained and excluded |
| How is the AI kept safe? | Shaurya | It only outputs a proposal (JSON). The controller validates it against policy; the AI has no shell or Docker access |
| Does it work on Macs? | Ayush / Inani | Yes: M2 and M5 via amd64 emulation (Rosetta / Colima); natively on amd64 |

## If something goes wrong

| Problem | Fix |
|---|---|
| Smoke test fails | `cd sandbox/hotel/.runtime; docker compose ps --all; docker compose logs --tail=50` |
| `refused: baseline already unhealthy` | That's the safety check working. Usually `rate` is still down: `docker compose start rate`, then rerun |
| A run is killed mid-fault | `python -m chaos.controller --recover` |
| Hotel stack won't come up | Fallback: `docker compose -f sandbox/dummy/docker-compose.yml up -d`, then `python -m chaos.controller experiments/dummy_web_stop.json` |
| Out of time | Skip section 6's live part; cut section 7 to one sentence |
