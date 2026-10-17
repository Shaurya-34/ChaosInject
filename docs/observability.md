# Observability (Prometheus)

Prometheus records what happens during a chaos run. It is **observability only**: every safety decision comes from the controller's own probe each second (`chaos/hooks.py`). Prometheus lags by a scrape interval and can go stale, so it never stops, starts or changes a run.

```text
controller (host) ──push 1/s──► Pushgateway :9091 ─┐
blackbox exporter ──probes /hotels every 2 s───────┼─► Prometheus :9090 (2-day retention)
cAdvisor ──per-container CPU/memory/network────────┘
```

## Run it

The hotel stack must be up first, because this stack joins its network.

```powershell
docker compose -f sandbox/observability/compose.yaml up -d
```

- Prometheus UI: <http://localhost:9090> (targets: Status → Targets)
- Pushgateway: <http://localhost:9091>
- Stop it: `docker compose -f sandbox/observability/compose.yaml stop`

All ports bind to 127.0.0.1. Images are pinned by digest. The controller pushes to `http://127.0.0.1:9091`; override it with `CHAOS_PUSHGATEWAY`, or set the variable to empty to disable pushing. If the Pushgateway isn't running, the run continues without metrics.

cAdvisor runs `privileged` (it reads container cgroups) and publishes no ports. It needs **v0.55+** on Docker Desktop with the containerd image store; v0.52 can't map containers there.

## Metrics

### Controller (`job="chaos-controller"`)

These exist only while a run is in progress. At the end, the controller pushes its final state, keeps it up for `CHAOS_METRICS_LINGER_S` (3 s by default) so it gets scraped, then deletes it from the Pushgateway. Every series has the labels `experiment`, `target` and `fault`.

| Metric | Meaning |
|---|---|
| `chaos_fault_active` | 1 while the fault is injected |
| `chaos_phase{phase}` | One-hot: `baseline`, `fault`, `recovery`, `done` |
| `chaos_probe_latency_ms` | Latency of the latest probe |
| `chaos_probe_ok` | 1 if the latest probe got HTTP status < 500 |
| `chaos_probe_status` | HTTP status of the latest probe; -1 on timeout or error |
| `chaos_probes_total{phase,result}` | Probes by phase and `ok`/`fail` |
| `chaos_safety_stop` | (`done` only) 1 if a safety rule ended the fault early |
| `chaos_recovered` | (`done` only) 1 if the app recovered after restore |
| `chaos_recovery_seconds` | (`done` only) restore → first ok probe |
| `chaos_stop_info{reason}` | (`done` only) why the fault phase ended |

### Blackbox (`job="blackbox-hotels"`)

`probe_success` and `probe_duration_seconds` for `GET http://frontend:5000/hotels?...`, probed from inside the hotel network every 2 s with a 1.9 s timeout. This is a second, independent view of the endpoint the controller probes.

### cAdvisor (`job="cadvisor"`)

The standard `container_*` metrics, kept only for `hotel-sandbox-*` and `chaos-dummy-*`. Filter by the `name` label, for example `name="hotel-sandbox-rate-1"`.

## Useful queries

```promql
# Fault window (use it to shade charts)
chaos_fault_active

# Controller probe latency vs. the independent probe
chaos_probe_latency_ms
probe_duration_seconds * 1000

# Did the endpoint fail in the last minute?
min_over_time(probe_success[1m])

# The target container's network and memory during the run
rate(container_network_transmit_packets_total{name="hotel-sandbox-rate-1"}[30s])
container_memory_working_set_bytes{name="hotel-sandbox-rate-1"}

# A crashed container (e.g. rate after its memcached is stopped) stops being seen
time() - container_last_seen{name="hotel-sandbox-rate-1"}
```

For a finished run, query the range `[fault_started - 10 s, fault_ended + recovery_s + 10 s]`, taking the times from `GET /api/v1/chaos/runs/{run_id}`.

## Verified

On Windows 11 (amd64, Docker Desktop, containerd image store), I ran `hotel_rate_loss` with all three targets up:

- `chaos_fault_active` went 0 → 1.
- `chaos_probe_latency_ms` went from about 20 ms to about 2000 ms.
- The blackbox `probe_duration_seconds` independently went from about 0.02 s to 1.5 s.
- `chaos_recovered` = 1 was scraped, and no `chaos_*` series were left in the Pushgateway afterwards.
- cAdvisor reported all 24 hotel containers.
