"""Time handling for the telemetry workstream.

Two clocks, never mixed:

* **Monotonic** (``mono()``) measures *durations* inside one process. It is immune
  to NTP steps and wall-clock adjustment. Every latency, window length and
  deadline in this package is derived from it.
* **UTC wall clock** (``now_utc()``) *labels* exported events so that markers from
  Tooshita's runtime process can be aligned with ours.

Because those two processes have independent wall clocks, any cross-process
alignment carries uncertainty. ``ClockUncertainty`` records it so a phase summary
can state how precisely its window boundaries line up with an injection marker,
rather than implying millisecond accuracy we do not have.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


def now_utc() -> datetime:
    """Timezone-aware UTC timestamp, for export and cross-process alignment."""
    return datetime.now(timezone.utc)


def iso(ts: datetime) -> str:
    """Canonical ISO-8601 UTC string."""
    if ts.tzinfo is None:
        raise ValueError("refusing to serialise a naive datetime; use now_utc()")
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def mono() -> float:
    """Monotonic seconds. Only meaningful as a difference, never as a date."""
    return time.monotonic()


@dataclass
class ClockUncertainty:
    """Records how well our clock aligns with another process's markers.

    ``observed_skew_ms`` is populated when the runtime includes both its own UTC
    timestamp and a round-trip measurement. Until that handshake happens the
    uncertainty is ``unknown`` and phase summaries say so rather than claiming
    exact window boundaries.
    """

    observed_skew_ms: float | None = None
    measurement_method: str = "unmeasured"
    notes: list[str] = field(default_factory=list)

    @property
    def is_known(self) -> bool:
        return self.observed_skew_ms is not None

    def describe(self) -> str:
        if not self.is_known:
            return "clock skew versus runtime process unmeasured; window edges approximate"
        return (
            f"clock skew versus runtime process ~{self.observed_skew_ms:.0f} ms "
            f"({self.measurement_method})"
        )


class Deadline:
    """A monotonic deadline. Used for probe timeouts and phase length limits."""

    __slots__ = ("_expires_at_mono",)

    def __init__(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError("deadline must be positive")
        self._expires_at_mono = mono() + seconds

    @property
    def expired(self) -> bool:
        return mono() >= self._expires_at_mono

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self._expires_at_mono - mono())
