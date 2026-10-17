# Fixing the cache crashes found by ChaosInject

## The weakness

Stopping `memcached-rate`, `memcached-profile` or `memcached-reserve` makes the matching service
(`rate`, `profile`, `reservation`) **crash**. In all three, any cache error reaches `log.Panic`. The
process exits, and `/hotels` fails until someone restarts the service by hand. A cache is supposed
to be optional; here it's a hard dependency that also takes its caller down.

## The fix (`prepare.py --fix-cache`)

Built from the pinned upstream source into `hotel-sandbox/{rate,profile,reservation}:<commit>-cache-fallback`:

1. **Fall back instead of crashing.** On a cache error, log a warning and read from MongoDB.
   `rate` and `profile` turn their panic branch into a fallback. `reservation` already reads
   MongoDB on a cache miss, so its cache calls go through wrappers
   ([`cache_breaker.go`](../sandbox/hotel/patches/cache_breaker.go)) that report an outage as a miss.
2. **Circuit breaker** ([`sandbox/hotel/patches/cache_breaker.go`](../sandbox/hotel/patches/cache_breaker.go)).
   With only the fallback, the service survives, but every request still waits for the 2 s
   cache timeout first, so `/hotels` times out anyway. After a cache error the breaker skips
   memcached for 10 s, so requests go straight to MongoDB.

`prepare.py` applies exact, checked string edits to copies of each `server.go` and never modifies
the upstream checkout. It refuses to build if the upstream code doesn't match.

```bash
python sandbox/hotel/prepare.py --fix-cache         # or without the flag for upstream behaviour
python scripts/dev.py up --execute                  # builds and starts the patched services
```

## Before and after

Each experiment stops the cache container for 20 s while `/hotels` is probed every second.

**`hotel_memcached_rate_stop`**

| | Upstream `rate` | Fallback only | Fallback + breaker |
|---|---|---|---|
| `rate` process | crashes; needs a manual restart | stays up | stays up |
| `/hotels` during the outage | fails | 5 of 6 probes fail (each waits for the 2 s cache timeout) | **17–19 of 18–19 OK, p95 78 ms** |
| Safety stop | fires | fires after 5 failed probes | not needed; runs its full duration |
| Recovery | manual | automatic | automatic |

**`hotel_memcached_profile_stop`**

| | Upstream `profile` | Fallback + breaker |
|---|---|---|
| `profile` process | crashes (exit 2); not recovered after 30 s | stays up |
| `/hotels` during the outage | 5 of 5 probes fail | **17 of 18 OK, p95 63 ms** |
| Safety stop | fires | not needed |

**`hotel_memcached_reserve_stop`**

| | Upstream `reservation` | Fallback + breaker |
|---|---|---|
| `reservation` process | crashes (exit 2); not recovered | stays up |
| `/hotels` during the outage | 5 of 5 probes fail | **17 of 18 OK, p95 63 ms** |
| Safety stop | fires | not needed |

The occasional failed probe is the single request per cooldown that tests whether the cache is back.

### A second bug behind the first

`reservation` has a separate upstream defect that an empty cache exposes. Its capacity lookup
queries MongoDB with `$in` at the top level of the filter, which MongoDB rejects. The nil cursor
then panics (`server.go:271`). It stays hidden while the capacity keys are cached, and appears
whenever the cache is empty: after the first start, or after the cache restarts. With only the
cache wrappers, `reservation` still crashed on the first outage. The patch also fixes the filter
(`{hotelId: {$in: [...]}}`).

### Limits

- **`review`** has the same `log.Panic` pattern, but it isn't patched: its `/review` route fails
  in this deployment even with a healthy cache (the discovery defect in
  [dependency findings](hotel-reservation-dependencies.md)), so a fix couldn't be verified.
- **Stale counts.** `reservation` caches room counts. If a cache outage keeps the cache's memory
  (for example a network partition rather than a restart), counts cached before the outage can
  be stale afterwards. A stopped container loses its memory, which is what these experiments do.
