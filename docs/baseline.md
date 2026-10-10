# Hotel Reservation steady-state baseline

Captured: 2026-10-10  
Sandbox: Hackathon PR #30 setup, using its pinned Hotel Reservation images  
Faults: none (normal steady-state run)

## Workload

- Target: `http://frontend:5000` from a container on `hotel-sandbox_default`
- wrk2: 2 threads, 10 connections, 50 requests/second, 60 seconds
- Lua workload: `hotel-reservation/mixed-workload_type_1.lua`
- Lua dependency: LuaSocket installed with `luarocks install luasocket`
- The Linux ARM64 wrk2 container builds its own Linux LuaJIT from the mounted source copy. The source mount is read-only.

Run the following from the DeathStarBench `hotelReservation` directory. The source path `../wrk2` and script path `wrk2/scripts` are relative to that directory.

```bash
docker run --rm --platform linux/arm64 --network hotel-sandbox_default \
  -v "$PWD/../wrk2:/source:ro" \
  -v "$PWD/wrk2/scripts:/scripts:ro" \
  ubuntu:24.04 bash -lc \
  'export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq build-essential libssl-dev zlib1g-dev luarocks ca-certificates >/dev/null && luarocks install luasocket >/dev/null && cp -R /source /tmp/wrk2 && cd /tmp/wrk2 && make clean && make && ./wrk -D exp -t 2 -c 10 -d 60s -R 50 -L -s /scripts/hotel-reservation/mixed-workload_type_1.lua http://frontend:5000'
```

## Results

| Machine / host | wrk2 container platform | Requests | Throughput | Average | p50 | p90 | p99 | p99.9 | Errors/timeouts |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| Shaurya's machine (amd64 host) | Not separately reported | 3,001 | 50.00 req/s | Not supplied | 4.6 ms | 11.8 ms | 17.4 ms | 22.9 ms | 0 reported |
| Krishna's MacBook Air M5 (arm64 host) | linux/arm64 | 3,001 | 50.00 req/s | 4.95 ms | 3.14 ms | 9.18 ms | 22.78 ms | 171.26 ms | No error/timeout lines in captured output; run completed at target throughput |

Krishna's app used the PR #30 pinned image, which the image lock identifies as linux/amd64. Docker Desktop had Rosetta enabled. The wrk2 generator ran as linux/arm64. The Mac result has a notably higher p99.9 tail than Shaurya's run; keep both sets of numbers when comparing machines.

## Interpretation limits

These are single steady-state runs, not repeated statistical samples. They record normal performance only; no chaos fault was active. Keep the two machines' measurements separate because their CPU architectures differ. The earlier 5 RPS run is excluded: its LuaSocket dependency was missing, and its connection/timeout settings produced timeouts.
