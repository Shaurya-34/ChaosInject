package PACKAGE

// ChaosInject patch (built only with `prepare.py --fix-cache`; PACKAGE is replaced per service).
//
// After a memcached error, the service skips the cache for a short cooldown and serves
// from MongoDB directly. Without this, every request during a cache outage waits for the full
// cache timeout (2 s) before falling back, which is as bad as an outage at the edge.

import (
	"sync/atomic"
	"time"

	"github.com/bradfitz/gomemcache/memcache"
	"github.com/rs/zerolog/log"
)

const cacheCooldown = 10 * time.Second

var cacheDownUntil int64 // unix nanoseconds; 0 means the cache is available

func cacheAvailable() bool {
	return time.Now().UnixNano() >= atomic.LoadInt64(&cacheDownUntil)
}

func markCacheDown() {
	atomic.StoreInt64(&cacheDownUntil, time.Now().Add(cacheCooldown).UnixNano())
}

// cacheGet, cacheGetMulti and cacheSet wrap the memcached client for services whose code already
// handles memcache.ErrCacheMiss by reading MongoDB: a cache outage is reported as a cache miss
// (and opens the breaker) instead of reaching the upstream log.Panic.

func cacheGet(c *memcache.Client, key string) (*memcache.Item, error) {
	if !cacheAvailable() {
		return nil, memcache.ErrCacheMiss
	}
	item, err := c.Get(key)
	if err != nil && err != memcache.ErrCacheMiss {
		log.Warn().Msgf("Memcached error for %v: %s; falling back to MongoDB", key, err)
		markCacheDown()
		return nil, memcache.ErrCacheMiss
	}
	return item, err
}

func cacheGetMulti(c *memcache.Client, keys []string) (map[string]*memcache.Item, error) {
	if !cacheAvailable() {
		return nil, memcache.ErrCacheMiss
	}
	items, err := c.GetMulti(keys)
	if err != nil && err != memcache.ErrCacheMiss {
		log.Warn().Msgf("Memcached error for %v: %s; falling back to MongoDB", keys, err)
		markCacheDown()
		return nil, memcache.ErrCacheMiss
	}
	return items, err
}

func cacheSet(c *memcache.Client, item *memcache.Item) {
	if cacheAvailable() {
		if err := c.Set(item); err != nil {
			markCacheDown()
		}
	}
}
