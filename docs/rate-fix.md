# Fixing the `rate` crash found by ChaosInject

## The weakness

Stopping `memcached-rate` makes the `rate` service **crash**. Any cache error reaches
`log.Panic` (upstream `services/rate/server.go:114`), the process exits, and `/hotels` fails until
someone restarts `rate` by hand. A cache is supposed to be optional; here it's a hard dependency
that also takes its caller down.

## The fix (`prepare.py --fix-rate`)

Two changes, built from the pinned upstream source into `hotel-sandbox/rate:<commit>-cache-fallback`:

1. **Fall back instead of crashing.** On a cache error, log a warning and read from MongoDB.
2. **Circuit breaker** ([`sandbox/hotel/patches/rate/cache_breaker.go`](../sandbox/hotel/patches/rate/cache_breaker.go)).
   With only the fallback, `rate` survives, but every request still waits for the 2 s cache
   timeout first, so `/hotels` times out anyway. After a cache error the breaker skips
   memcached for 10 s, so requests go straight to MongoDB.

`prepare.py` applies exact, checked string edits to a copy of `server.go` and never modifies
the upstream checkout. It refuses to build if the upstream code doesn't match.

```bash
python sandbox/hotel/prepare.py --fix-rate          # or without the flag for upstream behaviour
python scripts/dev.py up --execute                  # builds and starts the patched rate
```

## Before and after

`hotel_memcached_rate_stop`: memcached-rate stopped for 20 s, `/hotels` probed every second.

| | Upstream `rate` | Fallback only | Fallback + breaker |
|---|---|---|---|
| `rate` process | crashes; needs a manual restart | stays up | stays up |
| `/hotels` during the outage | fails | 5 of 6 probes fail (each waits for the 2 s cache timeout) | **17 of 18 OK, p95 78 ms** |
| Safety stop | fires | fires after 5 failed probes | not needed; runs its full duration |
| Recovery | manual | automatic | automatic |

The one failed probe is the single request per cooldown that tests whether the cache is back.

The same `log.Panic`-on-cache-error pattern exists in `profile`, `reservation` and `review`. This
patch covers `rate` only.
