"""Bounded retention and percentile estimation."""

from __future__ import annotations

import unittest

from backend.app.telemetry._contracts import Outcome
from backend.app.telemetry.config import SamplingConfig
from backend.app.telemetry.sampling import (
    BoundedRing,
    LatencyHistogram,
    OperationCounters,
    Sample,
)


def sample(t: float, *, ms: float | None = 100.0, outcome=Outcome.SUCCESS, op="browse"):
    return Sample(mono_t=t, operation=op, outcome=outcome, duration_ms=ms)


class BoundedRingTests(unittest.TestCase):
    def test_capacity_is_hard_and_evictions_are_counted(self):
        ring = BoundedRing(SamplingConfig(ring_capacity=10, ring_window_seconds=1e6))
        for i in range(100):
            ring.add(sample(float(i)))
        self.assertEqual(len(ring), 10)
        self.assertEqual(ring.dropped_by_capacity, 90)
        self.assertEqual(ring.dropped_total, 90)

    def test_age_eviction_bounds_the_window(self):
        ring = BoundedRing(SamplingConfig(ring_capacity=10_000, ring_window_seconds=120))
        for i in range(300):
            ring.add(sample(float(i)))
        ring.evict_older_than(now_mono=299.0)
        self.assertTrue(all(s.mono_t >= 179.0 for s in ring))
        self.assertGreater(ring.dropped_by_age, 0)

    def test_memory_stays_bounded_over_a_long_stream(self):
        """A 10k-sample stream must not grow the ring beyond its capacity."""
        config = SamplingConfig(ring_capacity=500, ring_window_seconds=120)
        ring = BoundedRing(config)
        for i in range(10_000):
            ring.add(sample(i * 0.01))
            ring.evict_older_than(now_mono=i * 0.01)
        self.assertLessEqual(len(ring), 500)

    def test_window_query_does_not_mutate(self):
        ring = BoundedRing(SamplingConfig(ring_capacity=100, ring_window_seconds=1e6))
        for i in range(50):
            ring.add(sample(float(i)))
        window = ring.window(now_mono=49.0, seconds=10.0)
        self.assertEqual(len(ring), 50)
        self.assertTrue(all(s.mono_t >= 39.0 for s in window))


class HistogramTests(unittest.TestCase):
    def test_quantile_is_within_stated_relative_error(self):
        values = list(range(1, 1001))
        hist = LatencyHistogram.from_values(float(v) for v in values)
        p95 = hist.quantile(0.95)
        self.assertIsNotNone(p95)
        # True p95 of 1..1000 is 950. Allow the documented bucket width plus a
        # small margin for midpoint placement.
        self.assertAlmostEqual(p95, 950.0, delta=950.0 * 0.05)

    def test_empty_histogram_returns_none_not_zero(self):
        self.assertIsNone(LatencyHistogram().quantile(0.95))

    def test_merge_matches_combined_population(self):
        """The reason merge exists: percentiles must never be averaged."""
        left = LatencyHistogram.from_values([10.0] * 100)
        right = LatencyHistogram.from_values([1000.0] * 100)

        merged = left.merge(right)
        merged_p95 = merged.quantile(0.95)
        direct = LatencyHistogram.from_values([10.0] * 100 + [1000.0] * 100)
        direct_p95 = direct.quantile(0.95)

        self.assertAlmostEqual(merged_p95, direct_p95, delta=direct_p95 * 0.05)

        # Averaging the two p95s would give ~505, which is not a real percentile
        # of any population. Confirm the merged answer is nowhere near it.
        naive_average = (left.quantile(0.95) + right.quantile(0.95)) / 2
        self.assertGreater(abs(merged_p95 - naive_average), 400)

    def test_overflow_is_counted_not_silently_clipped(self):
        hist = LatencyHistogram(SamplingConfig(histogram_max_ms=1000.0))
        hist.add(5000.0)
        self.assertEqual(hist.overflow_count, 1)

    def test_rejects_invalid_samples(self):
        hist = LatencyHistogram()
        for bad in (float("nan"), float("inf"), -1.0):
            with self.assertRaises(ValueError):
                hist.add(bad)

    def test_memory_is_independent_of_sample_count(self):
        small = LatencyHistogram()
        large = LatencyHistogram.from_values(float(i % 900) + 1 for i in range(100_000))
        self.assertEqual(small.memory_footprint_ints(), large.memory_footprint_ints())
        self.assertLess(small.memory_footprint_ints(), 1000)


class OperationCountersTests(unittest.TestCase):
    def test_timeouts_are_excluded_from_the_latency_population(self):
        counters = OperationCounters(operation="checkout")
        for _ in range(10):
            counters.record(sample(0.0, ms=100.0, op="checkout"))
        for _ in range(5):
            counters.record(
                sample(0.0, ms=None, outcome=Outcome.TIMEOUT, op="checkout")
            )

        self.assertEqual(counters.attempted, 15)
        self.assertEqual(counters.timed_out, 5)
        self.assertEqual(counters.success_latency.count, 10)

    def test_semantic_failure_counts_as_failure_not_success(self):
        counters = OperationCounters(operation="cart_read")
        counters.record(
            sample(0.0, ms=50.0, outcome=Outcome.SEMANTIC_FAILURE, op="cart_read")
        )
        self.assertEqual(counters.succeeded, 0)
        self.assertEqual(counters.failed, 1)
        # It still has a duration, so it belongs to the all-attempt population
        # but not the success-only one.
        self.assertEqual(counters.all_attempt_latency.count, 1)
        self.assertEqual(counters.success_latency.count, 0)

    def test_empty_window_success_rate_is_none_not_zero(self):
        counters = OperationCounters(operation="browse")
        self.assertIsNone(counters.success_rate)

    def test_counts_reconcile_after_merge(self):
        a = OperationCounters(operation="browse")
        b = OperationCounters(operation="browse")
        for _ in range(5):
            a.record(sample(0.0))
        for _ in range(3):
            b.record(sample(0.0, outcome=Outcome.HTTP_ERROR))
        merged = a.merge(b)
        self.assertEqual(merged.attempted, 8)
        self.assertEqual(merged.succeeded, 5)
        self.assertEqual(merged.failed, 3)

    def test_merging_different_operations_is_refused(self):
        with self.assertRaises(ValueError):
            OperationCounters(operation="a").merge(OperationCounters(operation="b"))


if __name__ == "__main__":
    unittest.main()
