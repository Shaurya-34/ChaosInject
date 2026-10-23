# Reference: command line, API and development

## Command line

The dashboard is optional; the controller runs on its own (start the sandbox first with `python scripts/dev.py up`):

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
target, fault, duration, probe URL and baseline samples. Experiments can't change the policy in
[`chaos/policy.json`](../chaos/policy.json).

## `scripts/dev.py`

| Command | Does |
|---|---|
| `setup` | creates `.venv`, installs the pinned Python dependencies, builds the dashboard (once) |
| `up [--execute]` | prepares and starts the hotel sandbox, Prometheus and the API/dashboard. `--execute` lets the dashboard inject real faults; without it, the dashboard is read-only |
| `up --llm MODEL` | also turns on LLM explanations; see [llm.md](llm.md) |
| `status` | what is running, the dashboard URL and the operator token |
| `down [--force]` | stops everything; refuses while a chaos run is active unless forced. Data volumes are kept |

`python sandbox/hotel/prepare.py --fix-cache` builds `rate`, `profile` and `reservation` with the
cache-outage fix instead of the upstream images; see [cache-fix.md](cache-fix.md).

## API

Served under `/api/v1/chaos`, with the Bearer operator token. Starting, stopping and recovering are
refused unless the API was started with execution enabled (`up --execute`).

| Endpoint | Does |
|---|---|
| `GET /environment` | first-run checks: execution enabled, sandbox answering, Prometheus ready, saved runs |
| `GET /experiments` | experiment files in `experiments/` |
| `POST /experiments/{id}/run` | starts the controller as a detached process (202) |
| `GET /active` | the in-flight run: running, exit code, saved `run_id`, leftover fault, log tail |
| `POST /active/stop` | kill switch: the controller stops and restores |
| `POST /recover` | restores a fault left by a killed controller |
| `GET /runs`, `GET /runs/{id}` | saved runs, shape in [`contracts/run.schema.json`](../contracts/run.schema.json) |
| `GET /runs/{id}/metrics` | Prometheus series for the run's window |
| `GET /score` | the resilience score, see [resilience-score.md](resilience-score.md) |
| `GET /proposals` | the injection agent's ranked next experiments |
| `GET /findings`, `GET /findings/report` | the monitoring agent's findings, as JSON or an HTML report |

The full OpenAPI contract is in [`contracts/v1/openapi.json`](../contracts/v1/openapi.json).

## Development

```bash
.venv/Scripts/python -m pytest -q                       # .venv/bin/python on Linux/macOS
.venv/Scripts/python scripts/export_contracts.py --check
cd frontend && npm run build && npm test
```

CI runs all of these on every pull request, and `main` requires it to pass. After changing the API,
run `python scripts/export_contracts.py` and commit the result.
