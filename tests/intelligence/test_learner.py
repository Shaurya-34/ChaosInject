import math
import os
import random
import tracemalloc
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.contracts import LearningFeatures, LearningObservation
from app.intelligence import fakes
from app.intelligence.features import FEATURE_NAMES, sanitize_features
from app.intelligence.learner import LearnerConfig, StreamingLearner
from app.intelligence.service import IntelligenceService

from .helpers import Req, run

_n = 0


def rid():
    global _n
    _n += 1
    return f"req_{_n}"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def feats(rng=None, rows=None, conn=0.1, fault=0.0):
    rng = rng or random.Random(0)
    rows = rows if rows is not None else rng.uniform(0, 10)
    return {
        "log_est_rows": rows,
        "log_est_bytes": rows + 4,
        "log_planner_cost": rows * 0.8,
        "has_where": float(rng.random() < 0.5),
        "plan_seq_scan": float(rows > 5),
        "db_conn_utilization": conn,
        "ready_fraction": 1.0,
        "fault_active": fault,
    }


def obs(request_id, kind, ms=None, source="live", features=None, fault_id=None):
    return LearningObservation(
        source=source, request_id=request_id, captured_at=datetime.now(timezone.utc), state_id="state_test", kind=kind,
        features=features, duration_ms=ms, output_rows=None, active_fault_id=fault_id,
    )


def true_ms(f, scale=1.0):
    return scale * math.expm1(0.6 * f["log_est_rows"] + 1.0)


def train(learner, n, rng, scale=1.0):
    for _ in range(n):
        r, f = rid(), feats(rng)
        learner.register_arrival(r, f, "live")
        learner.observe(obs(r, "completed", ms=true_ms(f, scale) * rng.uniform(0.9, 1.1)))


def test_prediction_happens_at_arrival_and_error_is_measured_before_update():
    learner = StreamingLearner()
    rng = random.Random(1)
    train(learner, 30, rng)

    r, f = rid(), feats(rng, rows=4.0)
    expected = learner.predict(f)
    learner.register_arrival(r, f, "live")
    assert math.isclose(math.expm1(learner._pending[r].model_prediction), expected.duration_ms, rel_tol=1e-9)

    train(learner, 40, rng, scale=3.0)  # the model moves on while r is pending

    calls = []
    model = learner._model
    real_learn = model.learn_one

    def spy_learn(x, y):
        calls.append("learn")
        return real_learn(x, y)

    class SpyEwma:
        def __init__(self, inner):
            self.inner = inner

        def update(self, e):
            calls.append("error")
            self.inner.update(e)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    model.learn_one = spy_learn
    learner._model_err = SpyEwma(learner._model_err)
    learner.observe(obs(r, "completed", ms=true_ms(f)))
    assert calls == ["error", "learn"]
    assert r not in learner._pending


def test_duplicate_outcome_does_not_update_twice():
    learner = StreamingLearner()
    r = rid()
    learner.register_arrival(r, feats(), "live")
    learner.observe(obs(r, "completed", ms=50))
    status = learner.observe(obs(r, "completed", ms=50))
    assert status.updates == 1 and learner.diagnostics()["counters"]["duplicate_outcome"] == 1
    learner.register_arrival(r, feats(), "live")
    assert learner.diagnostics()["counters"]["duplicate_arrival"] == 1


def test_pending_entries_expire_and_late_outcomes_are_unmatched():
    clock = Clock()
    learner = StreamingLearner(clock=clock)
    r = rid()
    learner.register_arrival(r, feats(), "live")
    assert learner.status().pending_count == 1
    clock.t += 121
    assert learner.status().pending_count == 0
    assert learner.diagnostics()["counters"]["expired_pending"] == 1
    assert learner.observe(obs(r, "completed", ms=10)).updates == 0
    assert learner.diagnostics()["counters"]["unmatched_outcome"] == 1


def test_censored_and_failed_outcomes_are_never_trained():
    learner = StreamingLearner()
    a, b = rid(), rid()
    learner.register_arrival(a, feats(), "live")
    learner.register_arrival(b, feats(), "live")
    learner.observe(obs(a, "censored", ms=999_999))
    status = learner.observe(obs(b, "failed", ms=5))
    c = learner.diagnostics()["counters"]
    assert status.updates == 0 and learner._trained == 0 and status.pending_count == 0
    assert c["censored"] == 1 and c["failed"] == 1


def test_fixture_observations_are_isolated_from_the_live_learner():
    live, fixture = StreamingLearner(source="live"), StreamingLearner(source="fixture")
    r = rid()
    live.register_arrival(r, feats(), "fixture")
    assert live.diagnostics()["counters"]["rejected_source"] == 1 and live.diagnostics()["counters"]["arrivals"] == 0
    fixture.register_arrival(r, feats(), "fixture")
    assert fixture.diagnostics()["counters"]["arrivals"] == 1


def test_telemetry_arrivals_with_shared_learning_features_are_accepted():
    learner = StreamingLearner()
    lf = LearningFeatures(
        estimated_output_bytes=2048, select_operation=True, has_join=False, has_sort=True,
        p95_ms=120.0, error_rate=0.0, connection_utilization=0.2, ready_fraction=1.0,
    )
    r = rid()
    learner.observe(obs(r, "arrival", features=lf))
    assert learner.status().pending_count == 1
    stored = dict(zip(FEATURE_NAMES, learner._pending[r].features))
    assert stored["plan_sort"] == 1.0 and stored["est_rows_missing"] == 1.0 and stored["db_conn_utilization"] == 0.2
    status = learner.observe(obs(r, "completed", ms=42))
    assert status.updates == 1


def test_queue_overflow_drops_without_blocking():
    learner = StreamingLearner(LearnerConfig(autodrain=False))
    for _ in range(300):
        learner.register_arrival(rid(), feats(), "live")
    assert learner.footprint()["queue"] == 256
    assert learner.diagnostics()["counters"]["queue_overflow"] == 44
    assert learner.drain() == 256 and learner.footprint()["queue"] == 0


def test_pending_map_is_bounded():
    learner = StreamingLearner()
    for _ in range(200):
        learner.register_arrival(rid(), feats(), "live")
    status = learner.status()
    assert status.pending_count == 128 and status.dropped_events == 72
    assert learner.diagnostics()["counters"]["evicted_pending"] == 72


def test_long_stream_memory_plateaus():
    clock = Clock()
    learner = StreamingLearner(clock=clock)
    rng = random.Random(7)
    ceilings = {"queue": 256, "pending": 128, "consumed_ids": 256, "coverage": 64, "moments": len(FEATURE_NAMES), "model_weights": len(FEATURE_NAMES), "scaler_means": len(FEATURE_NAMES)}

    def step(i):
        r, f = rid(), feats(rng)
        f["junk_" + str(i)] = 1.0  # unknown keys must not grow the model
        learner.register_arrival(r, f, "live")
        x = rng.random()
        if x < 0.6:
            learner.observe(obs(r, "completed", ms=true_ms(f)))
            learner.observe(obs(r, "completed", ms=true_ms(f)))  # duplicate
        elif x < 0.7:
            learner.observe(obs(r, "censored"))
        # else: never completes -> expiry/eviction
        learner.record_experiment(f"EXP-0{i % 3}", f"t{i % 200}")
        clock.t += 0.05
        sizes = learner.footprint()
        for key, ceiling in ceilings.items():
            assert sizes[key] <= ceiling, key

    tracemalloc.start()
    for i in range(5_000):
        step(i)
    mid, _ = tracemalloc.get_traced_memory()
    for i in range(5_000, 20_000):
        step(i)
    end, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert end - mid < 256 * 1024, f"memory grew by {end - mid} bytes after warm-up"
    status = learner.status()
    assert status.retained_raw_examples == 0 and status.persistence == "disabled"
    assert learner.diagnostics()["counters"]["coverage_overflow"] > 0


def test_restart_clears_state_and_writes_no_files(tmp_path, monkeypatch, allow):
    monkeypatch.chdir(tmp_path)
    svc = IntelligenceService(allow, learner=StreamingLearner(source="fixture"))
    est = fakes.FakePlanEstimator(allow)
    for i in range(50):
        run(svc.analyze_query(Req("SELECT id FROM lab.products WHERE id = $1", {"1": i}), fakes.state(), est))
    assert run(svc.get_learning_status()).pending_count == 50
    assert os.listdir(tmp_path) == []

    restarted = IntelligenceService(allow, learner=StreamingLearner(source="fixture"))
    status = run(restarted.get_learning_status())
    assert status.pending_count == 0 and status.updates == 0 and status.processed == 0


def test_cold_start_until_documented_sample_count():
    learner = StreamingLearner()
    rng = random.Random(3)
    assert learner.predict(feats(rng)).duration_ms is None
    train(learner, 19, rng)
    assert learner.predict(feats(rng)).cold_start
    train(learner, 1, rng)
    p = learner.predict(feats(rng))
    assert not p.cold_start and p.pre_update_sample_count == 20 and p.duration_ms is not None


def test_predict_then_learn_reports_model_and_baseline_error():
    learner = StreamingLearner()
    train(learner, 300, random.Random(5))
    status = learner.status()
    assert status.prediction_error is not None and learner.diagnostics()["baseline_error"] is not None
    assert status.drift_status == "stable"


def test_regime_shift_is_flagged_and_model_reset():
    learner = StreamingLearner()
    rng = random.Random(11)
    train(learner, 200, rng)
    assert learner.status().drift_status == "stable"
    for _ in range(60):
        train(learner, 1, rng, scale=200.0)
        if learner.diagnostics()["counters"]["model_resets"]:
            break
    assert learner.diagnostics()["counters"]["model_resets"] == 1
    assert learner.status().drift_status == "drift" and learner.diagnostics()["cold_start"]
    assert learner.predict(feats(rng)).quality_note.startswith("cold start")


def test_fault_time_state_does_not_shift_baseline_statistics():
    learner = StreamingLearner()
    for _ in range(20):
        learner.register_arrival(rid(), feats(conn=0.1), "live")
    before = learner.diagnostics()["ewma_db_conn_utilization_baseline"]
    for _ in range(20):
        learner.register_arrival(rid(), feats(conn=0.99, fault=1.0), "live", active_fault=True)
    assert learner.diagnostics()["ewma_db_conn_utilization_baseline"] == pytest.approx(before)


def test_invalid_observations_are_counted_not_trained():
    learner = StreamingLearner()
    learner.observe(obs("r1", "arrival"))  # arrival without features
    learner.register_arrival("r2", feats(), "live")
    status = learner.observe(obs("r2", "completed", ms=None))
    assert learner.diagnostics()["counters"]["invalid_observation"] == 2 and status.updates == 0


def test_feature_vector_is_fixed_and_clamped():
    x = sanitize_features({"log_est_rows": 1e9, "n_joins": -5, "made_up": 3.0, "ready_fraction": float("nan")})
    assert tuple(x) == FEATURE_NAMES
    assert x["log_est_rows"] == 25.0 and x["n_joins"] == 0.0 and x["ready_fraction"] == 0.0
    with pytest.raises(ValidationError):
        obs("r", "completed", ms=float("nan"))
