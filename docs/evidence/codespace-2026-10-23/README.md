# Evidence: ChaosInject on a clean machine (GitHub Codespace, 2026-10-23)

A second-machine test: a new GitHub Codespace, nothing installed by us, following the README. It shows that the
setup path works from zero, that the experiments inject and recover, and that the cache fix changes the outcome.
The raw logs and the saved run files are in this folder. Operator tokens are replaced with `<redacted>`.

## The machine

Ubuntu 24.04.5, x86_64, 4 cores, 16 GB RAM, 32 GB disk, Docker 29.8.0, Compose 5.5.1, Node 24.21.0. Default
`python3` was 3.14.2. Before the first `up`: 0 images, 0 volumes, 0 build cache (`environment.txt`). Code: `main` at
`7eb4413` (after PR #40).

## What was done and what happened

| Step | Result | Files |
|---|---|---|
| `dev.py setup` on the default Python 3.14 | **Failed**: building `pydantic-core` has no 3.14 support. Found, and fixed in PR #41 (a clear refusal, README says 3.11 to 3.13) | `1a-setup-python314-on-main-FAILS.log` |
| `dev.py setup` on Python 3.13 | 28 s, exit 0 | `1b-setup-python313.log` |
| `dev.py up --execute`, cold | **Failed**: 9 of 24 containers exited (`dial tcp ...:27017: i/o timeout`). Cause: this Codespace drops traffic between containers (plain `busybox` containers failed too), not ChaosInject. Fixed with one `sysctl`, written up in `docs/sandbox.md` | `2-up.log`, `codespace-network-note.txt`, `containers.txt` |
| `dev.py down` then `up --execute` after the `sysctl` | **24/24 sandbox and 4/4 observability containers**, dashboard and token printed, 32 s (images already pulled) | `4-up-after-network-fix.log`, `status-after-fix.txt` |
| Real runs, README quickstart (unpatched upstream images) | see below | `runs/` |
| `prepare.py --fix-cache`, then `up --execute` (the documented cache fix) | 24/24 and 4/4, 75 s including the image builds | `5-prepare-fix-cache.log`, `5-up-patched.log`, `containers-after-fix.txt` |
| The cache experiments again, patched | see below | `runs/after-*` |

After the first `up` the machine held 7 images (4.96 GB), 18 volumes (2.5 GB) and 1.7 GB of build cache, in line with
the README's disk figures.

## The experiments (real faults on the sandbox, from the controller)

| Experiment | Stop reason | During the fault | Recovered |
|---|---|---|---|
| `hotel_geo_stop` | safety: 5 consecutive failed probes | 5 of 5 failed | yes, 1.01 s |
| `hotel_rate_delay` | safety: p95 1204 ms against 200 ms, baseline p95 34 ms | latency rose | yes, 0.01 s |
| `hotel_memcached_rate_stop`, **unpatched** | safety: 5 consecutive failed probes | 5 of 5 failed | **no** (the `rate` service crashed and stayed down) |
| `hotel_memcached_rate_stop`, **patched** | duration elapsed | 0 of 20 failed | yes, 0.01 s |
| `hotel_memcached_profile_stop`, patched | duration elapsed | 0 of 20 failed | yes, 0.01 s |
| `hotel_memcached_reserve_stop`, patched | duration elapsed | 1 of 18 failed | yes, 0.01 s |

Resilience score: **33.3** (partial, 3 of 15 pairs) after the first three runs, **82.7** (partial, 5 of 15 pairs) after
the patched cache runs (`score.json`, `score-after-fix.json`). The score uses the latest run for each target and
fault pair, so the patched cache run replaced the failed one. After the unpatched crash the dashboard showed
"The hotel sandbox answers with an error (HTTP 500)" and the controller refused to inject again, which is the guard
rail working. Each run's full record (probes, policy, stop reason) is in `runs/*.json`; the controller's own output is
in `runs/*-log.txt`; the ranked findings are in `findings-after-fix.json`.

## Notes

- `steps.log` is the raw timeline in UTC. Its first block (13:53) is a script run that stopped early because
  `python3.13` was not on the PATH; the real run starts at 13:55. Lines that say "with the retry fix" refer to an
  `up` retry that was tried and then removed: it never fired, because the cause was the network.
- The Codespace needed one change that a normal machine does not:
  `sudo sysctl -w net.bridge.bridge-nf-call-iptables=0 net.bridge.bridge-nf-call-ip6tables=0`.
- Not covered: macOS, Windows Home without WSL, and the LLM explanations (no key was set here; those are covered by
  the live check on the author's machine, see `docs/llm.md`).
