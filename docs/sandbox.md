# Hotel Reservation sandbox

`python scripts/dev.py up` does all of this for you. This page explains what it runs and how to operate the sandbox by hand.

## Manual setup

Requires Git, Python 3.10+, Docker Engine/Desktop with a running **Linux** daemon,
and Docker Compose v2.29.7 or later. On Windows, Docker Desktop with Linux
containers and PowerShell works directly; a WSL2 shell is not required.
Reserve localhost ports 5000, 8500, and 16686.

The eight core application services pull the immutable
`deathstarbench/hotel-reservation` image. It is **linux/amd64**, verified from its
registry image configuration. Apple Silicon machines therefore need amd64
emulation enabled in Docker Desktop or their Colima VM; amd64 machines run it
natively. Review and attractions build from the pinned
upstream source using the host's default build platform. MongoDB, Memcached,
Consul, Jaeger, and the Go builder have arm64 and amd64 manifests.

From the repository root, use `python` on Windows (PowerShell) and `python3`
on Linux/macOS. The commands below use the Windows spelling; substitute
`python3` on Linux/macOS:

```powershell
docker info
docker compose version
python sandbox/hotel/prepare.py
cd sandbox/hotel/.runtime
docker compose pull --ignore-buildable
docker compose up -d --build
docker compose ps --all
python ../smoke.py
```

The first preparation clones DeathStarBench at commit
`6ecb09706140f8730b5385c08f1386c654c3c526`. It reads the **existing upstream
`hotelReservation/docker-compose.yml`** through Compose and derives a local
`compose.yaml`; it does not edit tracked upstream source files or invent services.
Generated files live under the gitignored `sandbox/hotel/.runtime/`.
Preparation stops if an existing upstream checkout has a different revision or
tracked modifications.

For Windows checkout compatibility, preparation enables repository-local
`core.longpaths=true` immediately after clone, then selects only
`hotelReservation` with `git sparse-checkout set --cone --sparse-index`
before checkout. This avoids invalid `*` filenames in `daprApps_v1/` and
supports long paths under `vendor/` and `helm-chart/`.
If an earlier preparation failed during checkout, move just
`sandbox/hotel/.runtime/DeathStarBench` to a backup location and rerun preparation.
Keep any local edits in that backup; there is no need to delete Docker volumes.

After preparation, plain `docker compose up -d --build` in that directory starts
the defined stack (only review and attractions have build definitions). Source
builds use the pinned Go base in a generated `Dockerfile.sandbox`. All registry
images, including MongoDB and the Go builder, use the immutable digests recorded
in [images.lock.json](../sandbox/hotel/images.lock.json). The lock records the
human-readable version used when resolving each digest:

| Image | Version resolved |
|---|---|
| deathstarbench/hotel-reservation | latest resolved to digest on 10 October 2026; amd64 only |
| hashicorp/consul | 1.17.3 |
| jaegertracing/all-in-one | 1.54.0 (legacy UDP agent supported) |
| memcached | 1.6.24 |
| mongo | 5.0.26 |
| golang | 1.21.13 |

Runtime configuration uses digests, **not floating latest tags**. Re-running
preparation does not refresh the lock. Any image upgrade requires an explicit
lock change and another smoke run. The pulled prebuilt application's source
revision is not established by this lock; source-built review/attractions use
the commit above.

## Readiness and verification

- Frontend: [http://localhost:5000](http://localhost:5000)
- Consul: [http://localhost:8500](http://localhost:8500)
- Jaeger UI: [http://localhost:16686](http://localhost:16686)

Ports bind only to 127.0.0.1. Database/cache ports stay internal. The generator
mounts the upstream config at `/workspace/config.json`, matching the verified
prebuilt image working directory and source Dockerfile. TLS is disabled for this
local sandbox; trace sampling is set to 1 for verification. Consul and MongoDB
health checks gate dependent service startup. Memcached runs with explicit
`-m 128 -t 2` flags. HTTP readiness is checked by the smoke script.

The smoke script checks all 24 containers are running, verifies actual Docker
restart policies, tests valid/invalid login, requires non-empty search and
recommendation results, creates **one real benchmark reservation**, verifies its
MongoDB record, and checks Jaeger UI plus a reported frontend tracing service.
It saves timestamped pass/fail evidence to `results/hotel-smoke-*.json` even if a
check fails. Reservation requests are not automatically retried.

This is a small API smoke run, not the fixed-rate wrk2 baseline (see [baseline.md](baseline.md)).
It uses seeded benchmark dates (2015-04-09 to 2015-04-10) and may consume one
room per successful invocation. It checks container state for review and
attractions, not their frontend RPC routes.

## Restart policy for chaos experiments

**Decision: disable automatic restart on every service in this sandbox.**
The generated model sets `restart: "no"` and removes upstream
`deploy.restart_policy.condition: any`. Docker must not undo a killed-container
fault before our safety controller observes it. A crashed startup stays visible;
inspect logs and fix the cause rather than relying on a restart loop.

The controller remains responsible for restoring its allowlisted target and
verifying recovery. This setup does not add Hotel containers to the controller's
allowlist automatically. Identify the exact container in this Compose project
before configuring an experiment:

```bash
docker compose ps --all
docker compose ps -q frontend
docker compose logs --tail=100 frontend search reservation
```

For manual recovery of a stopped container, use `docker compose start SERVICE`
inside this sandbox only. Starting an application again may rerun upstream
seed insertion. Do not assume the benchmark data is unchanged after a restart.

## Shutdown, data, and troubleshooting

```bash
# Stop and remove this project's containers/network, retaining MongoDB volumes:
docker compose down
# Start it again:
docker compose up -d --build
```

MongoDB data is persistent in project-specific `hotel-sandbox_*` named volumes.
Upstream initializers are not idempotent; repeated starts may duplicate seed
records. For a deliberately fresh benchmark, `docker compose down --volumes`
deletes this sandbox's database contents. Use that command only when you intend
to discard the local benchmark data; never use a global Docker prune.

On startup failure, inspect `docker compose ps --all` and
`docker compose logs --tail=100`. On an Apple Silicon execution-format failure,
check amd64 emulation in the Docker VM. On frontend smoke failures, keep the
saved evidence and logs; do not equate running containers or HTTP 200 with a
working booking flow. See the known source/configuration caveats in
[dependency findings](hotel-reservation-dependencies.md), including the
review/attractions discovery mismatch. This deployment does not silently patch
the pulled application binaries.

**GitHub Codespaces (and other nested Docker):** the Go services exit after 30 s with
`server selection timeout ... dial tcp 172.x.x.x:27017: i/o timeout` while MongoDB itself is healthy, and
`dev.py up` ends with "hotel frontend did not become ready". Traffic between containers on a bridge network is
dropped (it fails for two plain `busybox` containers too), because `br_netfilter` sends bridged traffic through
iptables rules that do not accept it. Fix it on the codespace, then run `dev.py down` and `up` again:

```bash
sudo sysctl -w net.bridge.bridge-nf-call-iptables=0 net.bridge.bridge-nf-call-ip6tables=0
```

## Validation status

The configuration was generated from the pinned upstream Compose file and
validated with standalone Docker Compose **v2.29.7**. Run the focused tests with
`python` on Windows or `python3` on Linux/macOS:

```powershell
python -m unittest discover -s tests -p 'test_hotel_setup.py' -v
```

**Verified end to end:** all 7 smoke checks pass on Windows 11 / Intel (amd64,
Docker Desktop) from a fresh clone. The whole README path, with real experiments, also ran on a clean
Ubuntu 24.04 / amd64 machine (a GitHub Codespace); see
[evidence/codespace-2026-10-23](evidence/codespace-2026-10-23/README.md). Apple Silicon runs the stack
under amd64 emulation (a baseline was captured on one, see [baseline.md](baseline.md)); it was not retested for
every later change.
