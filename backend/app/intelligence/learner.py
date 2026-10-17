"""Bounded, RAM-only streaming learner.

Two layers, kept distinct on purpose:

* Adaptive statistics (Day 1): arrival/decision counters, exponentially weighted
  feature moments, a moving-average duration baseline and bounded experiment
  coverage counts. These adapt but do not predict per request.
* Incremental predictor (Day 2): a fixed-feature River pipeline
  (StandardScaler -> LinearRegression) predicting log1p(duration_ms) of
  completed approved reads.

Processing order per request: predict at arrival from the decision-time feature
vector, park only the numeric vector under a random request ID, and when the
outcome arrives measure pre-update error, update once, then drop the entry.
Blocked requests never enter the pending map. Censored/failed outcomes are
counted but never trained on. Nothing is checkpointed; a restart starts empty.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal

from river import compose, linear_model, optim, preprocessing

from app.contracts import LearningObservation, LearningStatus, Prediction, Source

from .features import FEATURE_NAMES, FEATURE_SET_VERSION, from_learning_features, sanitize_features

LEARNER_VERSION = f"learner-1+{FEATURE_SET_VERSION}"
Kind = Literal["arrival", "completed", "censored", "failed"]
DriftStatus = Literal["cold_start", "stable", "drift", "unknown"]

_STATE_FEATURES = frozenset({"log_state_p95_ms", "state_error_rate", "db_conn_utilization", "ready_fraction"})
_COUNTERS = (
    "processed",
    "arrivals",
    "completed",
    "censored",
    "failed",
    "decisions_allow",
    "decisions_warn",
    "decisions_block",
    "duplicate_arrival",
    "duplicate_outcome",
    "unmatched_outcome",
    "expired_pending",
    "evicted_pending",
    "queue_overflow",
    "rejected_source",
    "invalid_observation",
    "model_resets",
    "coverage_overflow",
)


@dataclass(frozen=True)
class LearnerConfig:
    queue_limit: int = 256
    pending_limit: int = 128
    pending_ttl_seconds: float = 120.0
    consumed_id_limit: int = 256  # remembers recent random IDs only, to reject duplicates
    coverage_limit: int = 64
    metric_buffer_seconds: int = 120  # reported limit; the learner itself buffers no metric series
    intent_ttl_seconds: int = 60  # reported limit; intents live in the runtime vault
    model_limit_bytes: int = 65_536
    ewma_alpha: float = 0.1
    cold_start_samples: int = 20  # demo convention, not a reliability guarantee
    learning_rate: float = 0.02
    intercept_learning_rate: float = 0.1
    max_duration_ms: float = 600_000.0
    drift_min_samples: int = 30
    drift_short_alpha: float = 0.3
    drift_long_alpha: float = 0.03
    drift_ratio: float = 2.5
    drift_floor: float = 0.1
    drift_persistence: int = 8  # consecutive updates above ratio before flagging
    drift_action: Literal["reset", "flag"] = "reset"
    autodrain: bool = True


class _Ewma:
    __slots__ = ("alpha", "mean", "var", "n")

    def __init__(self, alpha: float):
        self.alpha = alpha
        self.mean = 0.0
        self.var = 0.0
        self.n = 0

    def update(self, x: float) -> None:
        if self.n == 0:
            self.mean = x
        else:
            delta = x - self.mean
            self.mean += self.alpha * delta
            self.var = (1 - self.alpha) * (self.var + self.alpha * delta * delta)
        self.n += 1


@dataclass(slots=True, frozen=True)
class _Event:
    kind: Kind
    request_id: str
    source: str
    vector: dict[str, float] | None = None  # arrivals only; already sanitized
    duration_ms: float | None = None
    active_fault: bool = False


@dataclass(slots=True)
class _Pending:
    features: tuple[float, ...]  # numeric vector only, in FEATURE_NAMES order
    model_prediction: float | None  # log1p(ms) predicted at arrival
    baseline_prediction: float | None  # moving-average baseline at arrival
    deadline: float  # monotonic seconds


class StreamingLearner:
    def __init__(
        self,
        config: LearnerConfig | None = None,
        *,
        source: Source = "live",
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config or LearnerConfig()
        self.source = source
        self._clock = clock
        self._lock = threading.RLock()
        self.started_at: datetime = datetime.now(timezone.utc)
        self._queue: deque[_Event] = deque()
        self._pending: OrderedDict[str, _Pending] = OrderedDict()
        self._consumed: OrderedDict[str, None] = OrderedDict()
        self._coverage: dict[tuple[str, str], int] = {}
        self.counters: dict[str, int] = {k: 0 for k in _COUNTERS}
        self._moments = {name: _Ewma(self.config.ewma_alpha) for name in FEATURE_NAMES}
        self._duration = _Ewma(self.config.ewma_alpha)
        self._reset_model()
        self._drift_flag = False

    # -- model lifecycle ----------------------------------------------------

    def _reset_model(self) -> None:
        self._model = compose.Pipeline(
            preprocessing.StandardScaler(),
            linear_model.LinearRegression(
                optimizer=optim.SGD(self.config.learning_rate),
                intercept_lr=self.config.intercept_learning_rate,
                clip_gradient=10.0,
            ),
        )
        self._trained = 0
        self._model_err = _Ewma(self.config.ewma_alpha)
        self._baseline_err = _Ewma(self.config.ewma_alpha)
        self._drift_short = _Ewma(self.config.drift_short_alpha)
        self._drift_long = _Ewma(self.config.drift_long_alpha)
        self._drift_streak = 0

    def _y_max(self) -> float:
        return math.log1p(self.config.max_duration_ms)

    def _predict_log(self, x: dict[str, float]) -> float | None:
        if self._trained == 0:
            return None
        y = self._model.predict_one(x)
        if y is None or not math.isfinite(y):
            return None
        return min(self._y_max(), max(0.0, y))

    # -- public API ---------------------------------------------------------

    def predict(self, features: dict[str, float]) -> Prediction:
        """Advisory prediction from decision-time features. Never mutates the model."""

        with self._lock:
            x = sanitize_features(features)
            y = self._predict_log(x)
            cold = self._trained < self.config.cold_start_samples
            drift = self._drift_status()
            if y is None:
                note = "cold start: no completed observations yet"
            elif cold:
                note = f"cold start: fewer than {self.config.cold_start_samples} completed observations; advisory only"
            elif drift == "drift":
                note = "drift suspected: residuals rising; low trust, advisory only"
            else:
                note = "advisory estimate from an online linear model; outside observed conditions it may be poor"
            return Prediction(
                duration_ms=None if y is None else math.expm1(y),
                learner_version=LEARNER_VERSION,
                pre_update_sample_count=self._trained,
                cold_start=cold,
                drift_status=drift,
                quality_note=note,
            )

    @staticmethod
    def trusted_prediction_ms(prediction: Prediction) -> float | None:
        """A prediction is usable for tightening policy only when warm and not drifting."""

        if prediction.cold_start or prediction.drift_status != "stable":
            return None
        return prediction.duration_ms

    def record_decision(self, decision: Literal["ALLOW", "WARN", "BLOCK"]) -> None:
        with self._lock:
            self.counters[f"decisions_{decision.lower()}"] += 1

    def record_experiment(self, catalog_id: str, target_id: str) -> None:
        with self._lock:
            key = (catalog_id, target_id)
            if key not in self._coverage and len(self._coverage) >= self.config.coverage_limit:
                self.counters["coverage_overflow"] += 1
                return
            self._coverage[key] = self._coverage.get(key, 0) + 1

    def coverage(self) -> dict[tuple[str, str], int]:
        with self._lock:
            return dict(self._coverage)

    def register_arrival(
        self, request_id: str, features: dict[str, float], source: str, active_fault: bool = False
    ) -> LearningStatus:
        """Arrival with the full decision-time vector (used by query analysis)."""

        return self._submit(
            _Event("arrival", request_id, source, vector=sanitize_features(features), active_fault=active_fault)
        )

    def observe(self, obs: LearningObservation) -> LearningStatus:
        """Shared-contract observation from telemetry/runtime."""

        vector = None
        if obs.kind == "arrival" and obs.features is not None:
            vector = from_learning_features(obs.features, obs.active_fault_id is not None)
        return self._submit(
            _Event(
                obs.kind,
                obs.request_id,
                obs.source,
                vector=vector,
                duration_ms=obs.duration_ms,
                active_fault=obs.active_fault_id is not None,
            )
        )

    def _submit(self, event: _Event) -> LearningStatus:
        with self._lock:
            if event.source != self.source:
                self.counters["rejected_source"] += 1
            elif len(self._queue) >= self.config.queue_limit:
                self.counters["queue_overflow"] += 1  # drop; never block the caller
            else:
                self._queue.append(event)
            if self.config.autodrain:
                self.drain()
            return self.status()

    def drain(self, max_items: int | None = None) -> int:
        with self._lock:
            processed = 0
            while self._queue and (max_items is None or processed < max_items):
                self._process(self._queue.popleft())
                processed += 1
            self._expire()
            return processed

    # -- processing ---------------------------------------------------------

    def _process(self, ev: _Event) -> None:
        self._expire()
        self.counters["processed"] += 1
        rid = ev.request_id
        if ev.kind == "arrival":
            if ev.vector is None:
                self.counters["invalid_observation"] += 1
                return
            if rid in self._pending or rid in self._consumed:
                self.counters["duplicate_arrival"] += 1
                return
            x = ev.vector
            self.counters["arrivals"] += 1
            fault = ev.active_fault or x["fault_active"] >= 0.5
            for name, value in x.items():
                # Faults must not normalise their own bad behaviour into the baseline.
                if fault and name in _STATE_FEATURES:
                    continue
                self._moments[name].update(value)
            if len(self._pending) >= self.config.pending_limit:
                self._pending.popitem(last=False)
                self.counters["evicted_pending"] += 1
            self._pending[rid] = _Pending(
                features=tuple(x[n] for n in FEATURE_NAMES),
                model_prediction=self._predict_log(x),
                baseline_prediction=self._duration.mean if self._duration.n else None,
                deadline=self._clock() + self.config.pending_ttl_seconds,
            )
            return

        entry = self._pending.pop(rid, None)
        if entry is None:
            key = "duplicate_outcome" if rid in self._consumed else "unmatched_outcome"
            self.counters[key] += 1
            return
        self._mark_consumed(rid)
        if ev.kind == "censored":
            self.counters["censored"] += 1  # truncated/cancelled: not a full-duration label
            return
        if ev.kind == "failed":
            self.counters["failed"] += 1
            return
        if ev.duration_ms is None or not math.isfinite(ev.duration_ms):
            self.counters["invalid_observation"] += 1
            return

        y = math.log1p(min(ev.duration_ms, self.config.max_duration_ms))
        x = dict(zip(FEATURE_NAMES, entry.features))
        # 1) measure error of the arrival-time prediction, before any update
        if entry.model_prediction is not None:
            err = abs(y - entry.model_prediction)
            self._model_err.update(err)
            self._update_drift(err)
        if entry.baseline_prediction is not None:
            self._baseline_err.update(abs(y - entry.baseline_prediction))
        # 2) learn exactly once from the original decision-time features
        self._model.learn_one(x, y)
        self._trained += 1
        if self._drift_flag and self._trained >= self.config.cold_start_samples:
            self._drift_flag = False
        self._duration.update(y)
        self.counters["completed"] += 1

    def _update_drift(self, err: float) -> None:
        self._drift_short.update(err)
        self._drift_long.update(err)
        if self._model_err.n < self.config.drift_min_samples:
            return
        threshold = self.config.drift_ratio * max(self._drift_long.mean, self.config.drift_floor)
        self._drift_streak = self._drift_streak + 1 if self._drift_short.mean > threshold else 0
        if self._drift_streak >= self.config.drift_persistence:
            if self.config.drift_action == "reset":
                self._drift_flag = True
                self.counters["model_resets"] += 1
                self._reset_model()

    def _drift_status(self) -> DriftStatus:
        # After a reset the flag holds until the fresh model leaves cold start.
        if self._drift_flag or self._drift_streak >= self.config.drift_persistence:
            return "drift"
        if self._trained < self.config.cold_start_samples:
            return "cold_start"
        if self._model_err.n < self.config.drift_min_samples:
            return "unknown"
        return "stable"

    def _expire(self) -> None:
        now = self._clock()
        while self._pending:
            rid, entry = next(iter(self._pending.items()))
            if entry.deadline > now:
                break
            del self._pending[rid]
            self.counters["expired_pending"] += 1  # a late outcome is then counted as unmatched

    def _mark_consumed(self, rid: str) -> None:
        self._consumed[rid] = None
        while len(self._consumed) > self.config.consumed_id_limit:
            self._consumed.popitem(last=False)

    # -- reporting ----------------------------------------------------------

    def status(self) -> LearningStatus:
        with self._lock:
            self._expire()
            return LearningStatus(
                source=self.source,
                learner_version=LEARNER_VERSION,
                started_at=self.started_at,
                processed=self.counters["processed"],
                updates=self.counters["completed"],
                pending_count=len(self._pending),
                dropped_events=self.counters["queue_overflow"] + self.counters["evicted_pending"],
                drift_status=self._drift_status(),
                prediction_error=self._model_err.mean if self._model_err.n else None,
                queue_limit=self.config.queue_limit,
                pending_limit=self.config.pending_limit,
                metric_buffer_seconds=self.config.metric_buffer_seconds,
                intent_ttl_seconds=self.config.intent_ttl_seconds,
                model_limit_bytes=self.config.model_limit_bytes,
            )

    def diagnostics(self) -> dict[str, object]:
        """Richer numeric state for docs and tests; still no raw examples."""

        with self._lock:
            m = self._moments
            return {
                "counters": dict(self.counters),
                "cold_start": self._trained < self.config.cold_start_samples,
                "baseline_error": self._baseline_err.mean if self._baseline_err.n else None,
                "trained_since_reset": self._trained,
                "ewma_duration_ms": math.expm1(self._duration.mean) if self._duration.n else None,
                "ewma_db_conn_utilization_baseline": m["db_conn_utilization"].mean if m["db_conn_utilization"].n else None,
                "experiment_coverage_entries": len(self._coverage),
            }

    def footprint(self) -> dict[str, int]:
        """Sizes of every growable structure, for bounded-memory tests."""

        with self._lock:
            lr = self._model.steps["LinearRegression"]
            scaler = self._model.steps["StandardScaler"]
            return {
                "queue": len(self._queue),
                "pending": len(self._pending),
                "consumed_ids": len(self._consumed),
                "coverage": len(self._coverage),
                "moments": len(self._moments),
                "model_weights": len(lr.weights),
                "scaler_means": len(scaler.means),
            }
