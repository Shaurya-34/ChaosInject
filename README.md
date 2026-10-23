# ChaosInject

[![Integration checks](https://github.com/Shaurya-34/ChaosInject/actions/workflows/ci.yml/badge.svg)](https://github.com/Shaurya-34/ChaosInject/actions/workflows/ci.yml)

Chaos engineering for a real microservice app, with guard rails. ChaosInject breaks things on
purpose (stops a container, adds network delay or packet loss) in a sandboxed 24-container hotel
booking system, measures what happens, **stops automatically** when a safety threshold is crossed,
always restores the fault, and ranks the weaknesses it found.

![Architecture](docs/img/architecture.svg)

## Find what you need

| I want to... | Go to |
|---|---|
| Run it | [Quickstart](#quickstart) |
| Open the dashboard | <http://127.0.0.1:8000> after `up`, see [The dashboard](#the-dashboard) |
| See how it fits together | [docs/architecture.md](docs/architecture.md) |
| See which services are tested | [Services under test](#services-under-test) and [docs/hotel-reservation-dependencies.md](docs/hotel-reservation-dependencies.md) |
| See the experiments | [Experiments](#experiments) |
| See what it found and fixed | [What it found](#what-it-found) and [docs/cache-fix.md](docs/cache-fix.md) |
| See it work on a clean machine | [docs/evidence/codespace-2026-10-23](docs/evidence/codespace-2026-10-23/README.md): logs and saved runs from a new Linux machine |
| Understand the resilience score | [docs/resilience-score.md](docs/resilience-score.md) |
| Check what keeps it safe | [Safety](#safety) and [docs/SAFETY.md](docs/SAFETY.md) |
| Turn on LLM explanations | [docs/llm.md](docs/llm.md) |
| Read the metrics | [docs/observability.md](docs/observability.md) |
| Use the command line or API, or develop | [docs/reference.md](docs/reference.md) |
| Set up or troubleshoot the sandbox by hand | [docs/sandbox.md](docs/sandbox.md), [baseline](docs/baseline.md), [dependency map](docs/dependency-map.json) ([diagram](docs/dependency-map.mmd)) |

## Quickstart

**Needs:** Git, Python 3.11 to 3.13 (CI runs 3.12; 3.14 does not work yet), Node.js 22+, and Docker Desktop/Engine (Linux containers, Compose
v2.29.7+; on Windows Home, Docker Desktop with the WSL 2 backend). Ports 5000, 8000, 8500, 9090, 9091
and 16686 on 127.0.0.1. The app images are amd64; on Apple Silicon enable amd64 emulation. The first
`up` pulls about 8 GB of images (layers are shared, so less on disk), builds two services from source
(2 to 5 GB of build cache) and creates 2.6 GB of data volumes: keep roughly 12 to 15 GB free. Use
`python3` instead of `python` on Linux/macOS.

```bash
git clone https://github.com/Shaurya-34/ChaosInject.git
cd ChaosInject
python scripts/dev.py setup          # venv, Python deps, dashboard build (once)
python scripts/dev.py up --execute   # sandbox + Prometheus + API/dashboard
```

The first `up` clones the pinned DeathStarBench source and pulls and builds images, so it takes a few
minutes. It ends by printing the dashboard URL and an **operator token**. Paste the token into the
dashboard, open **Chaos runs** and start an experiment. When you're done:

```bash
python scripts/dev.py down           # stop everything (data volumes are kept)
```

Without `--execute` the dashboard is read-only: you can browse, but starting, stopping and
recovering are refused. `python scripts/dev.py status` shows what's running and the token again.

## The dashboard

Open **Chaos runs**. A "Getting started" panel appears on a fresh clone and says what's missing.

- **Experiments:** run one, or press the Emergency STOP.
- **Runs:** every saved run; open one for the probe timeline and the Prometheus charts.
- **Resilience score:** one 0 to 100 number with its coverage, traceable to run ids.
- **Findings:** ranked weaknesses, each marked open, improved since or fixed since, with a downloadable report.
- **Suggested next experiments:** the injection agent's proposals. It only proposes; a person starts every run.

The two agents are rule-based. An optional LLM (Groq, or a local Ollama) can reword their explanation
text; it can't change a ranking, a score or a number. See [docs/llm.md](docs/llm.md).

## Services under test

[DeathStarBench Hotel Reservation](https://github.com/delimitrou/DeathStarBench/tree/master/hotelReservation)
(Go gRPC microservices), 24 containers on Docker Compose. The probe is `GET /hotels`, which fans out from
the frontend.

| Layer | Containers |
|---|---|
| Entry point | `frontend` (port 5000) |
| Request path for `/hotels` | `search` calls `geo` and `rate`; the frontend also calls `profile` and `reservation` |
| Other services | `recommendation`, `user`, `review`, `attractions` |
| Data | 8 MongoDB (one per service), 4 memcached (`rate`, `profile`, `reserve`, `review`) |
| Platform | `consul` (service discovery), `jaeger` (tracing) |

The policy allows faults only on a short list: `geo`, `rate` and the `rate`, `profile` and `reserve`
caches (plus a tiny test container). Everything else is refused.

## Experiments

Each file in [`experiments/`](experiments/) is one bounded fault, 20 to 30 seconds.

| Experiment | Fault | Target |
|---|---|---|
| `hotel_geo_stop` | container stop | `geo` |
| `hotel_rate_delay` | 200 ms network delay | `rate` |
| `hotel_rate_loss` | 30% packet loss | `rate` |
| `hotel_memcached_rate_stop` | container stop | `memcached-rate` |
| `hotel_memcached_profile_stop` | container stop | `memcached-profile` |
| `hotel_memcached_reserve_stop` | container stop | `memcached-reserve` |

## What it found

| Fault | What happened | Status |
|---|---|---|
| Stop a cache (`rate`, `profile` or `reservation`) | The service **crashes** (`log.Panic` on any cache error), `/hotels` fails and nothing recovers until it's restarted by hand | **Fixed**, opt-in: `prepare.py --fix-cache` falls back to MongoDB behind a circuit breaker. `/hotels` stays up, with at least 17 of 18 probes OK. The `reservation` fix also needed a second, separate upstream query bug fixed. See [docs/cache-fix.md](docs/cache-fix.md) |
| Stop `geo` | `/hotels` fails (a hard dependency) and recovers about 1 s after `geo` returns | Open: needs replicas or a fallback |
| 200 ms delay on `rate` | `/hotels` goes from 30 to 50 ms to **1.3 to 1.9 s, then times out**: one slow backend is amplified 6 to 10 times at the edge | Open: needs timeouts and a circuit breaker |
| 30% packet loss on `rate` | `/hotels` goes from 23 to 47 ms to **about 1.5 s with timeouts**: modest loss becomes a 30 times slowdown | Open: same remedy |

`review` has the same crash pattern but isn't patched: its `/review` route is already broken in this
deployment, so a fix couldn't be tested.

## Safety

- Runs only against the local sandbox. Every port binds to 127.0.0.1.
- Allowlisted containers and fault types only, with a maximum duration and a hard timeout.
- The controller probes every second and stops on its own if the error rate, consecutive failures or latency cross a threshold.
- One run at a time, a kill switch (the Stop button), and the fault is restored in a `finally` block. If the controller is killed mid-fault, the next run or **Recover** restores it.
- Observability and the AI agents never make a safety decision or touch Docker. Only the controller changes the system.

## Credits and license

Built by [Shaurya](https://github.com/Shaurya-34) and [Gautham](https://github.com/Gauthamop), starting from two
hackathon codebases (see the git history and [NOTICE](NOTICE)). The original hackathon runbooks are in
[docs/archive/](docs/archive/).

MIT. See [LICENSE](LICENSE). Code imported from AI-Chaos-Pilot is Apache-2.0; see [NOTICE](NOTICE) and
[LICENSE-APACHE](LICENSE-APACHE). The sandbox app is DeathStarBench (Apache-2.0).
