# Fixing the cache crashes found by ChaosInject

## The weakness

Stopping `memcached-rate` makes the `rate` service **crash**, and stopping `memcached-profile`
makes `profile` crash. In both, any cache error reaches `log.Panic`. The process exits, and
`/hotels` fails until someone restarts the service by hand. A cache is supposed to be optional;
here it's a hard dependency that also takes its caller down.

## The fix (`prepare.py --fix-cache`)

Two changes, built from the pinned upstream source into `hotel-sandbox/{rate,profile}:<commit>-cache-fallback`:

1. **Fall back instead of crashing.** On a cache error, log a warning and read from MongoDB.
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

The occasional failed probe is the single request per cooldown that tests whether the cache is back.

The same `log.Panic`-on-cache-error pattern also exists in `reservation` and `review`, but this
patch doesn't cover them yet. Their cache code is structured differently, and `memcached-reserve`
isn't in the policy allowlist, so it hasn't been tested.
