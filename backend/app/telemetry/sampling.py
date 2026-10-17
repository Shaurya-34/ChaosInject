"""Bounded sampling and percentile estimation.

Two structures, both fixed-memory by construction:

``BoundedRing``
    Keeps raw per-attempt samples for a short window (default 120 s) with a hard
    capacity. Used for the rolling ``SystemStateSnapshot`` and for debugging a
    live run. Evictions are counted, so a summary can say how many samples were
    dropped instead of silently under-reporting.

``LatencyHistogram``
    Log-bucket histogram used for phase percentiles. It is **mergeable**, which
    is the whole point: percentiles over a combined window are computed by
    merging bucket counts, never by averaging two p95 values. Averaging
    percentiles is not an approximation, it is simply wrong, and it is the most
    common way a resilience report ends up with numbers nobody can defend.

Approximation, stated plainly: buckets are geometric with a 2% relative width,
so a reported quantile is within about 2% of the true sample quantile, and the
value returned is the geometric midpoint of the containing bucket. Memory is
~600 integers regardless of how many samples pass through.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from ._contracts import Outcome
from .config import SamplingConfig


@dataclass(frozen=True, slots=True)
class Sample:
    """One attempt. ``duration_ms`` is monotonic-derived, never wall-clock delta.

    ``duration_ms`` is ``None`` for attempts that never completed (a timeout has
    no completion time), which keeps them out of the latency population.
    """

    mono_t: float
    operation: str
    outcome: Outcome
    duration_ms: float | None
    error_category: str | None = None

    @property
    def is_success(self) -> bool:
        return self.outcome is Outcome.SUCCESS


class BoundedRing:
    """Fixed-capacity, time-windowed sample buffer.

    Bounded twice over: by ``capacity`` and by ``window_seconds``. Whichever
    binds first wins, so a traffic spike cannot grow memory and a quiet period
    cannot keep stale samples alive.
    """

    def __init__(self, config: SamplingConfig | None = None) -> None:
        self._config = config or SamplingConfig()
        self._buf: deque[Sample] = deque(maxlen=self._config.ring_capacity)
        self._dropped_by_capacity = 0
        self._dropped_by_age = 0

    def add(self, sample: Sample) -> None:
        if len(self._buf) == self._buf.maxlen:
            self._dropped_by_capacity += 1
        self._buf.append(sample)

    def evict_older_than(self, *, now_mono: float) -> None:
        cutoff = now_mono - self._config.ring_window_seconds
        while self._buf and self._buf[0].mono_t < cutoff:
            self._buf.popleft()
            self._dropped_by_age += 1

    def window(self, *, now_mono: float, seconds: float) -> list[Sample]:
        """Samples from the last ``seconds``. Does not mutate the ring."""
        cutoff = now_mono - seconds
        return [s for s in self._buf if s.mono_t >= cutoff]

    def __len__(self) -> int:
        return len(self._buf)

    def __iter__(self) -> Iterator[Sample]:
        return iter(self._buf)

    @property
    def dropped_total(self) -> int:
        """Samples evicted. Reported in ``DataQuality`` so loss is visible."""
        return self._dropped_by_capacity + self._dropped_by_age

    @property
    def dropped_by_capacity(self) -> int:
        return self._dropped_by_capacity

    @property
    def dropped_by_age(self) -> int:
        return self._dropped_by_age

    @property
    def capacity(self) -> int:
        return int(self._buf.maxlen or 0)


class LatencyHistogram:
    """Mergeable log-bucket histogram over milliseconds.

    Values below ``min_ms`` land in bucket 0; values above ``max_ms`` land in a
    terminal overflow bucket and are counted in ``overflow_count`` so a summary
    can disclose that the upper tail was clipped rather than silently flattening
    it.
    """

    def __init__(self, config: SamplingConfig | None = None) -> None:
        cfg = config or SamplingConfig()
        self._min_ms = cfg.histogram_min_ms
        self._max_ms = cfg.histogram_max_ms
        self._rel = cfg.histogram_relative_error
        self._growth = 1.0 + self._rel
        self._log_growth = math.log(self._growth)
        self._bucket_count = (
            int(math.ceil(math.log(self._max_ms / self._min_ms) / self._log_growth)) + 1
        )
        self._buckets = [0] * self._bucket_count
        self._count = 0
        self._overflow = 0
        self._min_observed: float | None = None
        self._max_observed: float | None = None

    # -- construction ----------------------------------------------------

    @classmethod
    def from_values(
        cls, values: Iterable[float], config: SamplingConfig | None = None
    ) -> "LatencyHistogram":
        hist = cls(config)
        for v in values:
            hist.add(v)
        return hist

    def _index(self, value_ms: float) -> int:
        if value_ms <= self._min_ms:
            return 0
        return min(
            self._bucket_count - 1,
            int(math.log(value_ms / self._min_ms) / self._log_growth),
        )

    def _bucket_midpoint(self, index: int) -> float:
        lo = self._min_ms * (self._growth**index)
        hi = lo * self._growth
        return math.sqrt(lo * hi)

    # -- mutation --------------------------------------------------------

    def add(self, value_ms: float) -> None:
        if value_ms < 0 or math.isnan(value_ms) or math.isinf(value_ms):
            raise ValueError(f"invalid latency sample: {value_ms!r}")
        if value_ms > self._max_ms:
            self._overflow += 1
        self._buckets[self._index(value_ms)] += 1
        self._count += 1
        self._min_observed = (
            value_ms if self._min_observed is None else min(self._min_observed, value_ms)
        )
        self._max_observed = (
            value_ms if self._max_observed is None else max(self._max_observed, value_ms)
        )

    def merge(self, other: "LatencyHistogram") -> "LatencyHistogram":
        """Combine two histograms. This is how windows are joined.

        Percentiles over the combined population come from the merged bucket
        counts. There is no path in this package that averages two percentiles.
        """
        if self._bucket_count != other._bucket_count or self._rel != other._rel:
            raise ValueError("cannot merge histograms with different bucket layouts")
        merged = LatencyHistogram.__new__(LatencyHistogram)
        merged._min_ms = self._min_ms
        merged._max_ms = self._max_ms
        merged._rel = self._rel
        merged._growth = self._growth
        merged._log_growth = self._log_growth
        merged._bucket_count = self._bucket_count
        merged._buckets = [a + b for a, b in zip(self._buckets, other._buckets)]
        merged._count = self._count + other._count
        merged._overflow = self._overflow + other._overflow
        mins = [v for v in (self._min_observed, other._min_observed) if v is not None]
        maxs = [v for v in (self._max_observed, other._max_observed) if v is not None]
        merged._min_observed = min(mins) if mins else None
        merged._max_observed = max(maxs) if maxs else None
        return merged

    # -- query -----------------------------------------------------------

    def quantile(self, q: float) -> float | None:
        """Estimated quantile, or ``None`` when there is nothing to estimate.

        The caller is responsible for the minimum-sample rule; this returns a
        value for any non-empty histogram and ``None`` only when empty.
        """
        if not 0.0 < q < 1.0:
            raise ValueError("quantile must be strictly between 0 and 1")
        if self._count == 0:
            return None
        target = q * self._count
        cumulative = 0
        for index, bucket in enumerate(self._buckets):
            if bucket == 0:
                continue
            cumulative += bucket
            if cumulative >= target:
                return round(self._bucket_midpoint(index), 3)
        return round(self._bucket_midpoint(self._bucket_count - 1), 3)

    @property
    def count(self) -> int:
        return self._count

    @property
    def overflow_count(self) -> int:
        return self._overflow

    @property
    def min_observed_ms(self) -> float | None:
        return self._min_observed

    @property
    def max_observed_ms(self) -> float | None:
        return self._max_observed

    @property
    def method(self) -> str:
        """Human-readable description, exported on every aggregate."""
        return (
            f"bounded log-bucket histogram, {self._rel:.0%} relative bucket width, "
            f"{self._bucket_count} buckets over {self._min_ms:.0f}-{self._max_ms:.0f} ms, "
            "bucket geometric midpoint, mergeable"
        )

    def memory_footprint_ints(self) -> int:
        return self._bucket_count


@dataclass
class OperationCounters:
    """Running counts for one operation inside one phase."""

    operation: str
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    timed_out: int = 0
    error_categories: dict[str, int] = field(default_factory=dict)
    success_latency: LatencyHistogram = field(default_factory=LatencyHistogram)
    all_attempt_latency: LatencyHistogram = field(default_factory=LatencyHistogram)

    def record(self, sample: Sample) -> None:
        self.attempted += 1
        if sample.outcome is Outcome.TIMEOUT:
            self.timed_out += 1
        elif sample.is_success:
            self.succeeded += 1
        else:
            self.failed += 1

        if sample.error_category:
            self.error_categories[sample.error_category] = (
                self.error_categories.get(sample.error_category, 0) + 1
            )

        if sample.duration_ms is not None:
            self.all_attempt_latency.add(sample.duration_ms)
            if sample.is_success:
                self.success_latency.add(sample.duration_ms)

    def merge(self, other: "OperationCounters") -> "OperationCounters":
        if self.operation != other.operation:
            raise ValueError("cannot merge counters for different operations")
        merged = OperationCounters(operation=self.operation)
        merged.attempted = self.attempted + other.attempted
        merged.succeeded = self.succeeded + other.succeeded
        merged.failed = self.failed + other.failed
        merged.timed_out = self.timed_out + other.timed_out
        merged.error_categories = dict(self.error_categories)
        for key, value in other.error_categories.items():
            merged.error_categories[key] = merged.error_categories.get(key, 0) + value
        merged.success_latency = self.success_latency.merge(other.success_latency)
        merged.all_attempt_latency = self.all_attempt_latency.merge(
            other.all_attempt_latency
        )
        return merged

    @property
    def success_rate(self) -> float | None:
        """``None`` when nothing was attempted -- an empty window is unknown, not 0%."""
        if self.attempted == 0:
            return None
        return self.succeeded / self.attempted
