"""``SystemStateSnapshot`` construction and freshness.

This is the object Tooshita's stop policy reads, so its contract matters more
than its implementation:

* **Stale is not healthy.** ``valid_until`` is short for safety-critical fields.
  A consumer reading past it must treat the snapshot as unavailable. The
  decision of what to do about that belongs to the stop policy; our job is to
  make the distinction impossible to miss.
* **Unknown is not zero.** Any metric we could not obtain is absent from the
  numeric fields and named in ``missing_features``. A null error rate means we
  could not measure it, which is a reason to be cautious, whereas ``0.0`` would
  read as perfect health.
* **A percentile below the sample minimum is not published.** Same rule as phase
  summaries, so the number on the live dashboard and the number in the report
  come from the same policy.

The snapshot also carries ``active_fault_id`` so the learner can label an
observation as baseline or injected-fault rather than guessing from timing.
"""

from __future__ import annotations

from datetime import timedelta

from ._contracts import Source, SystemStateSnapshot, new_id
from .clock import mono, now_utc
from .cluster import ClusterAdapter, ClusterReadout
from .config import SamplingConfig, StateConfig
from .sampling import BoundedRing, LatencyHistogram


class StateBuilder:
    """Builds snapshots from the rolling sample ring plus a cluster readout."""

    def __init__(
        self,
        sandbox_id: str,
        ring: BoundedRing,
        cluster: ClusterAdapter,
        *,
        state_config: StateConfig | None = None,
        sampling_config: SamplingConfig | None = None,
        source: Source = Source.LIVE,
    ) -> None:
        self._sandbox_id = sandbox_id
        self._ring = ring
        self._cluster = cluster
        self._state = state_config or StateConfig()
        self._sampling = sampling_config or SamplingConfig()
        self._source = source
        self._active_fault_id: str | None = None
        self._db_used: int | None = None
        self._db_limit: int | None = None

    # -- external facts ----------------------------------------------------

    def set_active_fault(self, fault_id: str | None) -> None:
        """Called by the run orchestrator when a fault is injected or removed."""
        self._active_fault_id = fault_id

    def set_db_connections(self, used: int | None, limit: int | None) -> None:
        """Connection-pool facts, when a database target is in play.

        Left as ``None`` when no database is involved or the restricted role
        cannot read ``pg_stat_activity``.
        """
        self._db_used = used
        self._db_limit = limit

    # -- snapshot ----------------------------------------------------------

    async def capture(self) -> SystemStateSnapshot:
        captured_at = now_utc()
        now_mono = mono()
        window = self._state.snapshot_window_seconds
        samples = self._ring.window(now_mono=now_mono, seconds=window)

        missing: list[str] = []

        readout: ClusterReadout = await self._cluster.read()
        missing.extend(readout.missing_features)

        if samples:
            attempts = len(samples)
            failures = sum(1 for s in samples if not s.is_success)
            request_rate = attempts / window
            error_rate = failures / attempts
            hist = LatencyHistogram.from_values(
                [s.duration_ms for s in samples if s.is_success and s.duration_ms is not None],
                self._sampling,
            )
            if hist.count >= self._sampling.min_samples_for_percentile:
                p95 = hist.quantile(0.95)
            else:
                p95 = None
                missing.append(
                    f"p95_ms: {hist.count} successful samples in the last "
                    f"{window:.0f}s is below the minimum of "
                    f"{self._sampling.min_samples_for_percentile}"
                )
        else:
            attempts = 0
            request_rate = None
            error_rate = None
            p95 = None
            missing.append(
                f"request_rate, error_rate, p95_ms: no samples in the last {window:.0f}s "
                "(probe not running, or no traffic offered)"
            )

        if self._db_used is None or self._db_limit is None:
            missing.append("db_connections_used / db_connections_limit")

        # Safety-critical fields govern the expiry of the whole snapshot. If the
        # consumer can only trust the numbers for five seconds, the object says
        # five seconds -- it does not advertise the longer context window.
        valid_until = captured_at + timedelta(
            seconds=self._state.critical_freshness_seconds
        )

        return SystemStateSnapshot(
            state_id=new_id("state"),
            source=self._source,
            captured_at=captured_at,
            valid_until=valid_until,
            sandbox_id=self._sandbox_id,
            ready_by_workload=readout.ready_by_workload,
            request_rate=round(request_rate, 4) if request_rate is not None else None,
            error_rate=round(error_rate, 6) if error_rate is not None else None,
            p95_ms=p95,
            db_connections_used=self._db_used,
            db_connections_limit=self._db_limit,
            cpu_utilization=readout.cpu_utilization,
            memory_utilization=readout.memory_utilization,
            missing_features=sorted(set(missing)),
            active_fault_id=self._active_fault_id,
            sample_count=attempts,
        )


def state_is_usable_for_safety(
    snapshot: SystemStateSnapshot,
    *,
    at,
    required_fields: tuple[str, ...] = ("error_rate", "p95_ms"),
) -> tuple[bool, str]:
    """Can a stop policy rely on this snapshot right now?

    Returns ``(usable, reason)``. We do not decide what the policy should *do*
    about an unusable snapshot -- that is Tooshita's call -- but we make certain
    the policy can tell "healthy" apart from "we cannot see".
    """
    if not snapshot.is_fresh(at=at):
        age = (at - snapshot.captured_at).total_seconds()
        return False, f"snapshot expired at {snapshot.valid_until.isoformat()} (age {age:.1f}s)"

    absent = [
        field for field in required_fields if getattr(snapshot, field, None) is None
    ]
    if absent:
        return False, "required safety metrics unavailable: " + ", ".join(absent)

    return True, "fresh and complete"
