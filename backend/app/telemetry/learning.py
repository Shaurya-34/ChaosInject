"""Bounded, RAM-only channel feeding Gautam's streaming learner.

This is where it would be easiest to accidentally build a training dataset, so
the constraints are enforced in code rather than left as a convention:

* **No persistence.** There is no file handle in this module. ``persistence`` is
  reported as ``disabled`` in the status object and a test asserts that running a
  long synthetic stream writes nothing to disk.
* **Bounded queue and bounded pending map.** Both have hard capacities. Overflow
  increments a drop counter that is reported, rather than growing memory or
  blocking the caller. The safety channel must stay responsive even when the
  learner is behind, so backpressure here never propagates to the probe.
* **Features are fixed at arrival.** They come from the state snapshot at
  decision time. Outcome events carry no features and are joined by
  ``request_id``. Substituting post-execution state into a training record would
  leak the outcome into its own inputs -- the model would look excellent and
  predict nothing.
* **Censored is not completed.** A cancelled or capped job observed 4 seconds of
  a query that would have taken 40. Learning on it as a completed sample teaches
  the model that expensive queries are cheap. ``CENSORED`` carries a reason and
  the learner decides how to handle the censoring.
* **Blocked and failed queries have no duration at all.** The model enforces
  this; there is no path that attaches a duration to a failed observation.

Observations are also labelled ``baseline`` or ``injected_fault`` from the active
fault ID, so chaos-window data never contaminates the healthy baseline.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterator

from ._contracts import LearningKind, LearningObservation, Source, SystemStateSnapshot
from .clock import now_utc
from .config import LearningChannelConfig


@dataclass
class ChannelStats:
    """Counters the learner publishes in ``LearningStatus``."""

    emitted: int = 0
    dropped_queue_full: int = 0
    dropped_pending_full: int = 0
    expired_pending: int = 0
    joined_outcomes: int = 0
    orphan_outcomes: int = 0
    retained_raw_examples: int = 0  # structurally always zero
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, int | str]:
        return {
            "emitted": self.emitted,
            "dropped_queue_full": self.dropped_queue_full,
            "dropped_pending_full": self.dropped_pending_full,
            "expired_pending": self.expired_pending,
            "joined_outcomes": self.joined_outcomes,
            "orphan_outcomes": self.orphan_outcomes,
            "retained_raw_examples": self.retained_raw_examples,
            "persistence": "disabled",
        }


@dataclass(frozen=True)
class PendingArrival:
    """An arrival awaiting its outcome. Lives in RAM, expires on a TTL."""

    request_id: str
    state_id: str | None
    captured_at: datetime
    active_fault_id: str | None
    baseline_context: str


class LearningChannel:
    """Bounded producer side of the learning stream.

    The consumer (Gautam) drains via ``drain`` or subscribes with ``on_emit``.
    Nothing is retained after draining.
    """

    def __init__(
        self,
        config: LearningChannelConfig | None = None,
        *,
        source: Source = Source.LIVE,
        on_emit: Callable[[LearningObservation], None] | None = None,
    ) -> None:
        self._config = config or LearningChannelConfig()
        if self._config.persistence_enabled:
            raise ValueError(
                "persistence must stay disabled; a durable observation store is "
                "exactly the retained training dataset this design forbids"
            )
        self._source = source
        self._on_emit = on_emit
        self._queue: deque[LearningObservation] = deque(
            maxlen=self._config.queue_capacity
        )
        self._pending: OrderedDict[str, PendingArrival] = OrderedDict()
        self.stats = ChannelStats()

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _context_for(active_fault_id: str | None) -> str:
        return "injected_fault" if active_fault_id else "baseline"

    def _expire_pending(self, *, now: datetime) -> None:
        ttl = timedelta(seconds=self._config.pending_ttl_seconds)
        cutoff = now - ttl
        while self._pending:
            key, arrival = next(iter(self._pending.items()))
            if arrival.captured_at >= cutoff:
                break
            self._pending.pop(key)
            self.stats.expired_pending += 1

    def _push(self, observation: LearningObservation) -> bool:
        if len(self._queue) == self._config.queue_capacity:
            # Drop the oldest and count it. Bounded memory beats completeness
            # here, and the counter makes the loss visible to the learner.
            self._queue.popleft()
            self.stats.dropped_queue_full += 1
        self._queue.append(observation)
        self.stats.emitted += 1
        if self._on_emit is not None:
            self._on_emit(observation)
        return True

    # -- producer API ------------------------------------------------------

    def record_arrival(
        self,
        request_id: str,
        features: dict[str, float],
        state: SystemStateSnapshot | None,
        *,
        confounders: tuple[str, ...] = (),
    ) -> LearningObservation:
        """Record the decision-time features for a request.

        ``features`` and ``state`` are captured **now**, before execution. They
        are never revised when the outcome arrives.
        """
        now = now_utc()
        self._expire_pending(now=now)

        active_fault = state.active_fault_id if state else None
        context = self._context_for(active_fault)

        if len(self._pending) >= self._config.pending_capacity:
            self._pending.popitem(last=False)
            self.stats.dropped_pending_full += 1

        self._pending[request_id] = PendingArrival(
            request_id=request_id,
            state_id=state.state_id if state else None,
            captured_at=now,
            active_fault_id=active_fault,
            baseline_context=context,
        )

        observation = LearningObservation(
            request_id=request_id,
            source=self._source,
            captured_at=now,
            state_id=state.state_id if state else None,
            kind=LearningKind.ARRIVAL,
            features=dict(features),
            active_fault_id=active_fault,
            baseline_context=context,  # type: ignore[arg-type]
            confounders=list(confounders),
        )
        self._push(observation)
        return observation

    def _outcome(
        self,
        request_id: str,
        kind: LearningKind,
        *,
        duration_ms: float | None = None,
        rows_returned: int | None = None,
        bytes_returned: int | None = None,
        censoring_reason: str | None = None,
        confounders: tuple[str, ...] = (),
    ) -> LearningObservation:
        now = now_utc()
        self._expire_pending(now=now)

        arrival = self._pending.pop(request_id, None)
        if arrival is None:
            self.stats.orphan_outcomes += 1
            state_id = None
            active_fault = None
            context = "unknown"
        else:
            self.stats.joined_outcomes += 1
            state_id = arrival.state_id
            active_fault = arrival.active_fault_id
            context = arrival.baseline_context

        observation = LearningObservation(
            request_id=request_id,
            source=self._source,
            captured_at=now,
            state_id=state_id,
            kind=kind,
            features=None,
            duration_ms=duration_ms,
            rows_returned=rows_returned,
            bytes_returned=bytes_returned,
            active_fault_id=active_fault,
            baseline_context=context,  # type: ignore[arg-type]
            confounders=list(confounders),
            censoring_reason=censoring_reason,
        )
        self._push(observation)
        return observation

    def record_completed(
        self,
        request_id: str,
        *,
        duration_ms: float,
        rows_returned: int,
        bytes_returned: int,
        confounders: tuple[str, ...] = (),
    ) -> LearningObservation:
        """A job that ran to completion within its budgets."""
        return self._outcome(
            request_id,
            LearningKind.COMPLETED,
            duration_ms=duration_ms,
            rows_returned=rows_returned,
            bytes_returned=bytes_returned,
            confounders=confounders,
        )

    def record_censored(
        self,
        request_id: str,
        *,
        observed_ms: float,
        reason: str,
        rows_returned: int | None = None,
        bytes_returned: int | None = None,
        confounders: tuple[str, ...] = (),
    ) -> LearningObservation:
        """A cancelled, capped or truncated job.

        ``observed_ms`` is a **lower bound** on the full duration, not the
        duration. The learner must treat it as censored.
        """
        return self._outcome(
            request_id,
            LearningKind.CENSORED,
            duration_ms=observed_ms,
            rows_returned=rows_returned,
            bytes_returned=bytes_returned,
            censoring_reason=reason,
            confounders=confounders,
        )

    def record_failed(
        self, request_id: str, *, confounders: tuple[str, ...] = ()
    ) -> LearningObservation:
        """A job that errored, or a query that was blocked before execution.

        Carries no duration: there is no execution time to learn from.
        """
        return self._outcome(
            request_id, LearningKind.FAILED, confounders=confounders
        )

    # -- consumer API ------------------------------------------------------

    def drain(self) -> list[LearningObservation]:
        """Remove and return everything queued. Nothing is retained."""
        items = list(self._queue)
        self._queue.clear()
        return items

    def __len__(self) -> int:
        return len(self._queue)

    def __iter__(self) -> Iterator[LearningObservation]:
        return iter(self._queue)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def status(self) -> dict[str, int | str]:
        payload = self.stats.as_dict()
        payload["pending_count"] = self.pending_count
        payload["queue_capacity"] = self._config.queue_capacity
        payload["pending_capacity"] = self._config.pending_capacity
        payload["queue_depth"] = len(self._queue)
        return payload
