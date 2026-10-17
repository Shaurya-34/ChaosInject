package rate

// ChaosInject patch (built only with `prepare.py --fix-rate`).
//
// After a memcached error, the rate service skips the cache for a short cooldown and serves
// from MongoDB directly. Without this, every request during a cache outage waits for the full
// cache timeout (2 s) before falling back, which is as bad as an outage at the edge.

import (
	"sync/atomic"
	"time"
)

const cacheCooldown = 10 * time.Second

var cacheDownUntil int64 // unix nanoseconds; 0 means the cache is available

func cacheAvailable() bool {
	return time.Now().UnixNano() >= atomic.LoadInt64(&cacheDownUntil)
}

func markCacheDown() {
	atomic.StoreInt64(&cacheDownUntil, time.Now().Add(cacheCooldown).UnixNano())
}
