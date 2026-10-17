# ChaosInject

[![Integration checks](https://github.com/Shaurya-34/ChaosInject/actions/workflows/ci.yml/badge.svg)](https://github.com/Shaurya-34/ChaosInject/actions/workflows/ci.yml)

Chaos engineering for a real microservice app, with guard rails. ChaosInject injects **controlled
failures** (container stops, network delay, packet loss) into a sandboxed 24-container hotel
booking system, measures the impact live, **stops automatically** when safety thresholds are
breached, always restores the fault, and explains which weaknesses to fix first.

- **Controller:** baseline → inject → probe every second → safety stop → restore → verify recovery.
- **Dashboard:** start and stop experiments, watch them live, inspect every run with Prometheus charts.
- **Agents:** an injection agent proposes the next experiments from the dependency map and past runs;
  a monitoring agent turns runs into ranked findings and an HTML report. They recommend; they never execute.
  Rule-based, with an optional LLM (Groq or a local Ollama) for the explanation text: [docs/llm.md](docs/llm.md).
- **Safety first:** allowlisted targets and faults only, a hard timeout, a kill switch, and a
  restore that runs even if the run fails.

![Architecture](docs/img/architecture.svg)

## What it found

Real results on [DeathStarBench Hotel Reservation](https://github.com/delimitrou/DeathStarBench/tree/master/hotelReservation):

| Experiment | Result |
|---|---|
| Stop `memcached-rate` | `/hotels` fails, and **the `rate` service crashes**: it calls `log.Panic` on any cache error (`services/rate/server.go:114`). It doesn't recover until restarted by hand. The same pattern exists in profile, reservation and review |
| Stop `geo` | `/hotels` fails (a hard dependency) and recovers about 1 s after `geo` returns |
| 200 ms network delay on `rate` | `/hotels` goes from 30–50 ms to **1.3–1.9 s, then times out (>2 s)**: one slow backend amplifies 6–10× at the edge. The safety stop fires; it recovers instantly once the delay is removed |
| 30% packet loss on `rate` | `/hotels` goes from 23–47 ms to **~1.5 s, with timeouts**: lost packets become TCP retransmission waits, so modest loss on one backend becomes a 30× slowdown at the edge. The safety stop fires; it recovers instantly once the loss is removed |

## Quickstart

**Needs:** Git, Python 3.10+, Node.js 22+, and Docker Desktop/Engine (Linux containers, Compose
v2.29.7+). Ports 5000, 8000, 8500, 9090, 9091 and 16686 on 127.0.0.1. The app images are
amd64; on Apple Silicon enable amd64 emulation. Use `python3` instead of `python` on Linux/macOS.

```bash
git clone https://github.com/Shaurya-34/ChaosInject.git
cd ChaosInject
python scripts/dev.py setup          # venv, Python deps, dashboard build (once)
python scripts/dev.py up --execute   # sandbox + Prometheus + API/dashboard
```

The first `up` clones the pinned DeathStarBench source and pulls/builds images, so it takes a few
minutes. It ends by printing the dashboard URL (<http://127.0.0.1:8000>) and an **operator token**:
paste the token into the dashboard, open **Chaos runs**, and start an experiment.

```bash
python scripts/dev.py status   # what's running, plus the token again
python scripts/dev.py down     # stop everything (data volumes are kept)
```

Without `--execute` the dashboard is read-only: you can browse runs and findings, but starting,
stopping and recovering are refused.

## How a run works

1. **Policy check.** The experiment's target and fault must be in [`chaos/policy.json`](chaos/policy.json),
   and its duration within limits. Experiments can't change the policy.
2. **Baseline.** The controller probes `/hotels` and refuses to inject if the app is already unhealthy.
3. **Inject** the fault: `docker stop`, or [Pumba](https://github.com/alexei-led/pumba) netem for delay and loss.
4. **Probe every second.** It stops early if the error rate, consecutive failures or latency cross a
   threshold, or if the operator presses Stop.
5. **Restore** the fault in a `finally` block, then **verify recovery** and save the run to `results/`.

Each run also pushes live metrics to Prometheus (via the Pushgateway), next to an independent
blackbox probe and cAdvisor container metrics. Observability never makes a safety decision.
Details: [architecture](docs/architecture.md), [safety](docs/SAFETY.md), [observability](docs/observability.md).

## Safety

- Runs only against the local sandbox; every port binds to 127.0.0.1.
- Allowlisted containers and fault types only; max duration and a hard timeout from the policy.
- One run at a time (a run lock), and a stop file as a kill switch, used by the dashboard's Stop button.
- If the controller is killed mid-fault, a marker file records it; the next run or **Recover** restores it.
- The app must be confirmed healthy after every experiment.
- The AI agents only propose. A human starts every run.

## Command line

The dashboard is optional; the controller runs on its own:

```bash
python -m chaos.controller experiments/hotel_geo_stop.json
python -m chaos.controller experiments/hotel_memcached_rate_stop.json
python -m chaos.controller experiments/hotel_rate_delay.json
python -m chaos.controller experiments/hotel_rate_loss.json
python -m chaos.controller --recover     # restore a fault left by a killed controller
python -m chaos.hooks list               # past runs
python -m chaos.hooks show <run_id>
```

Ctrl+C stops a run; the fault is always restored. Each experiment file in `experiments/` sets the
target, fault, duration, probe URL and baseline samples.

## API

Served under `/api/v1/chaos`, with the Bearer operator token:

| Endpoint | Does |
|---|---|
| `GET /experiments` | experiment files in `experiments/` |
| `POST /experiments/{id}/run` | starts the controller as a detached process (202) |
| `GET /active` | the in-flight run: running, exit code, saved `run_id`, leftover fault, log tail |
| `POST /active/stop` | kill switch: the controller stops and restores |
| `POST /recover` | restores a fault left by a killed controller |
| `GET /runs`, `GET /runs/{id}` | saved runs, shape in [`contracts/run.schema.json`](contracts/run.schema.json) |
| `GET /runs/{id}/metrics` | Prometheus series for the run's window |
| `GET /proposals` | the injection agent's ranked next experiments |
| `GET /findings`, `GET /findings/report` | the monitoring agent's findings, as JSON or an HTML report |

The full OpenAPI contract is in [`contracts/v1/openapi.json`](contracts/v1/openapi.json).

## Development

```bash
.venv/Scripts/python -m pytest -q                       # .venv/bin/python on Linux/macOS
.venv/Scripts/python scripts/export_contracts.py --check
cd frontend && npm run types && npm run build && npm test
```

CI runs all of these on every pull request.

## Docs

- [Architecture](docs/architecture.md): components, one run end to end, safety design
- [Safety controller and results store](docs/SAFETY.md)
- [Observability](docs/observability.md): Prometheus, Pushgateway, blackbox, cAdvisor; metric names and PromQL
- [Hotel Reservation sandbox](docs/sandbox.md): manual setup, image pinning, restart policy, troubleshooting
- [Dependency findings](docs/hotel-reservation-dependencies.md) and [dependency map](docs/dependency-map.json) ([diagram](docs/dependency-map.mmd))
- [Baseline](docs/baseline.md): wrk2 steady-state latency
- [Archive](docs/archive/): the original hackathon runbooks and pitch

## Credits and license

Built by [Shaurya](https://github.com/Shaurya-34) and [Gautham](https://github.com/Gauthamop), starting
from two hackathon codebases (see the git history and [NOTICE](NOTICE)).

MIT. See [LICENSE](LICENSE). Code imported from AI-Chaos-Pilot is Apache-2.0; see [NOTICE](NOTICE)
and [LICENSE-APACHE](LICENSE-APACHE). The sandbox app is DeathStarBench (Apache-2.0).
