"""Telemetry tunables.

Every threshold the telemetry workstream depends on lives here, so the demo,
the tests and the findings report all read the same numbers, and so a reviewer
can see each decision in one place.

Values marked ``TEAM CONVENTION`` are our own choices, not standards. They are
published in ``docs/telemetry/README.md`` and stated in the findings report.
Values marked ``TBD -- calibrate`` must be measured against the pinned
application before any reported run; the probe refuses to start until the
markers are calibrated.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SCHEMA_VERSION = "1.0"

# Calculation version. Bump whenever a formula or normalisation changes, so a
# report and a UI value can be proven to come from the same arithmetic.
SCORING_VERSION = "score-1.0.0"


@dataclass(frozen=True)
class SamplingConfig:
    """Bounded in-RAM retention for raw samples."""

    # TEAM CONVENTION: raw per-request samples live at most this long.
    ring_window_seconds: float = 120.0
    # Hard cap derived from the capped offered rate:
    # 120 s * 50 rps * 1.5 headroom = 9000. Memory is bounded regardless of load.
    ring_capacity: int = 9000
    # TEAM CONVENTION: a percentile is only published with at least this many
    # samples in the window. Below it, percentile fields are null with a reason.
    min_samples_for_percentile: int = 100
    # Log-bucket histogram relative width. 2% buckets from 1 ms to 120 s.
    histogram_relative_error: float = 0.02
    histogram_min_ms: float = 1.0
    histogram_max_ms: float = 120_000.0


@dataclass(frozen=True)
class StateConfig:
    """Freshness windows for ``SystemStateSnapshot``."""

    # Safety-critical metrics must be recent or they are treated as unavailable.
    # Tooshita's stop policy reads this; stale is never interpreted as healthy.
    critical_freshness_seconds: float = 5.0
    # Non-critical context (resource utilisation) may be older before expiry.
    context_freshness_seconds: float = 30.0
    # Rolling window used to compute request_rate / error_rate / p95 for a snapshot.
    snapshot_window_seconds: float = 30.0


@dataclass(frozen=True)
class RecoveryConfig:
    """Sustained-recovery detection.

    A restored replica count alone does not prove business recovery, so the
    detector also requires successful critical transactions for a sustained
    interval before declaring recovery verified.
    """

    window_seconds: float = 10.0
    # TEAM CONVENTION: this many consecutive healthy windows.
    consecutive_healthy_windows: int = 3
    # Success rate must be within this absolute tolerance of the frozen baseline.
    success_rate_tolerance: float = 0.02
    # p95 must be within this multiple of the frozen baseline p95.
    p95_regression_factor: float = 1.5
    # Give up after this long and report an observed failure to recover.
    timeout_seconds: float = 180.0
    # Minimum successful critical transactions per window to count as healthy.
    min_successful_critical_per_window: int = 3


@dataclass(frozen=True)
class LearningChannelConfig:
    """Bounded, RAM-only channel feeding Gautam's streaming learner."""

    queue_capacity: int = 2_000
    # Arrival records waiting to be joined to an outcome.
    pending_capacity: int = 1_000
    pending_ttl_seconds: float = 300.0
    # Never changes. Writing observations to disk would create exactly the
    # retained training dataset the design forbids.
    persistence_enabled: bool = False


@dataclass(frozen=True)
class WorkloadConfig:
    """Synthetic workload shape.

    Start conservative and measure the laptop's capacity before raising these.
    An offered rate the machine cannot sustain invalidates every comparison.
    """

    load_profile_id: str = "boutique-mixed-v1"
    generator_version: str = "telemetry-probe-1.0.0"
    concurrency: int = 8
    requested_rate_rps: float = 10.0
    warmup_seconds: float = 20.0
    request_timeout_seconds: float = 5.0
    # Operation mix must sum to 1.0; validated on construction.
    operation_mix: dict[str, float] = field(
        default_factory=lambda: {
            "browse": 0.40,
            "product_view": 0.25,
            "add_to_cart": 0.20,
            "cart_read": 0.10,
            "checkout": 0.05,
        }
    )
    # Operations whose failure means the business workflow is broken. Recovery
    # detection requires these to succeed, not merely for pods to be Ready.
    critical_operations: tuple[str, ...] = ("add_to_cart", "cart_read", "checkout")


@dataclass(frozen=True)
class TelemetryConfig:
    sampling: SamplingConfig = field(default_factory=SamplingConfig)
    state: StateConfig = field(default_factory=StateConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    learning: LearningChannelConfig = field(default_factory=LearningChannelConfig)
    workload: WorkloadConfig = field(default_factory=WorkloadConfig)


DEFAULT_CONFIG = TelemetryConfig()
