"""Mocked unit tests for Tooshita local demo runtime (no real HTTP target required)."""
import pytest
from app.api.errors import PortError
from app.runtime.constants import MAX_FAULT_DURATION_SECONDS
from app.runtime.demo_contract import DemoInjectionRequest
from app.runtime.demo_guard import DemoRuntimeGuard
from app.runtime.executor import MutationExecutor
from app.runtime.health import HealthProbeResult, LoopbackHealthClient, utcnow
from app.runtime.latch import StopLatchStore
from tests.conftest import OPERATOR_TOKEN, auth_headers


class MockHealthClient(LoopbackHealthClient):
    def __init__(self, *, status="healthy", requests=1, stale=False):
        super().__init__("http://mock.invalid/health")
        self._status = status
        self._requests = requests
        self._stale = stale

    async def probe_fresh(self) -> HealthProbeResult:
        return HealthProbeResult(
            status=self._status,
            requests=self._requests,
            probed_at=utcnow(),
            stale=self._stale,
        )


def guard(tmp_path, **kwargs) -> DemoRuntimeGuard:
    store = StopLatchStore(tmp_path / "latch.json")
    return DemoRuntimeGuard(latch_store=store, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("duration,allowed", [(1, True), (30, True), (60, True), (61, False), (600, False)])
async def test_user_duration_boundary_without_dispatch(tmp_path, duration, allowed):
    executor = MutationExecutor()
    runtime = guard(tmp_path, health_client=MockHealthClient(), executor=executor)
    result = await runtime.inject_attempt(DemoInjectionRequest(fault_type="service_pause", duration_seconds=duration), "tester")
    assert result.decision == ("ALLOW" if allowed else "BLOCK")
    assert result.popup_heading == ("Safety check passed" if allowed else "Error has occurred")
    assert runtime.latched is (not allowed)
    assert result.stop_latched is (not allowed)
    assert result.executed is False and result.mutations_dispatched == executor.dispatch_calls == 0
    if allowed:
        assert not runtime.latch_store.path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("health", [MockHealthClient(status="unavailable"), MockHealthClient(stale=True)])
async def test_safe_duration_still_requires_fresh_health(tmp_path, health):
    runtime = guard(tmp_path, health_client=health)
    result = await runtime.inject_attempt(DemoInjectionRequest(fault_type="service_pause", duration_seconds=30), "tester")
    assert result.decision == "BLOCK" and result.rule_id == "TARGET_UNAVAILABLE"
    assert runtime.latched and runtime.executor.dispatch_calls == 0


@pytest.mark.parametrize("duration", [0, -1, 601, 1.5, "30", True, None])
def test_invalid_durations_rejected_by_api(demo_settings, duration):
    from starlette.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app(settings=demo_settings)) as client:
        response = client.post("/api/v1/demo/injection", headers=auth_headers(), json={"fault_type": "service_pause", "duration_seconds": duration})
        assert response.status_code == 422
        assert client.get("/api/v1/demo/safety", headers=auth_headers()).json()["stop_latched"] is False


@pytest.mark.asyncio
async def test_ninety_second_request_blocked_against_sixty_second_cap(tmp_path):
    executor = MutationExecutor()
    runtime = guard(tmp_path, health_client=MockHealthClient(), executor=executor)
    body = DemoInjectionRequest(fault_type="service_pause", duration_seconds=90)
    result = await runtime.inject_attempt(body, "tester")
    assert result.decision == "BLOCK"
    assert result.rule_id == "DURATION_HARD_LIMIT"
    assert result.executed is False
    assert result.mutations_dispatched == 0
    assert result.duration_seconds == 90
    assert result.maximum_seconds == MAX_FAULT_DURATION_SECONDS
    assert result.request_id
    assert executor.dispatch_calls == 0


@pytest.mark.asyncio
async def test_executor_spy_never_dispatched(tmp_path):
    executor = MutationExecutor()
    runtime = guard(tmp_path, health_client=MockHealthClient(), executor=executor)
    await runtime.inject_attempt(DemoInjectionRequest(fault_type="service_pause", duration_seconds=90), "tester")
    await runtime.inject_attempt(DemoInjectionRequest(fault_type="service_pause", duration_seconds=30), "tester")
    assert executor.dispatch_calls == 0


@pytest.mark.asyncio
async def test_target_healthy_before_and_after_mocked_attempt(tmp_path):
    client = MockHealthClient(status="healthy", requests=4)
    runtime = guard(tmp_path, health_client=client)
    before = await runtime.probe()
    result = await runtime.inject_attempt(
        DemoInjectionRequest(fault_type="service_pause", duration_seconds=90),
        "tester",
    )
    after = await runtime.probe()
    assert before["status"] == "healthy"
    assert after["status"] == "healthy"
    assert result.target_status == "healthy"


@pytest.mark.asyncio
async def test_repeat_attempts_blocked_while_stop_latched(tmp_path):
    runtime = guard(tmp_path, health_client=MockHealthClient())
    first = await runtime.inject_attempt(
        DemoInjectionRequest(fault_type="service_pause", duration_seconds=90),
        "tester",
    )
    second = await runtime.inject_attempt(
        DemoInjectionRequest(fault_type="service_pause", duration_seconds=90),
        "tester",
    )
    assert first.rule_id == "DURATION_HARD_LIMIT"
    assert second.rule_id == "STOP_LATCHED"
    assert runtime.latched is True


@pytest.mark.asyncio
async def test_reset_succeeds_with_fresh_healthy_target(tmp_path):
    runtime = guard(tmp_path, health_client=MockHealthClient())
    await runtime.inject_attempt(
        DemoInjectionRequest(fault_type="service_pause", duration_seconds=90),
        "tester",
    )
    summary = await runtime.reset("local-demo", "tester")
    assert summary.recovery_status == "verified"
    assert runtime.latched is False


@pytest.mark.asyncio
async def test_reset_fails_when_health_unavailable(tmp_path):
    runtime = guard(tmp_path, health_client=MockHealthClient(status="unavailable"))
    with pytest.raises(PortError):
        await runtime.reset("local-demo", "tester")


@pytest.mark.asyncio
async def test_reset_fails_when_health_stale(tmp_path):
    runtime = guard(tmp_path, health_client=MockHealthClient(status="healthy", stale=True))
    await runtime.inject_attempt(
        DemoInjectionRequest(fault_type="service_pause", duration_seconds=90),
        "tester",
    )
    with pytest.raises(PortError):
        await runtime.reset("local-demo", "tester")


@pytest.mark.asyncio
async def test_stop_latch_persists_across_guard_restart(tmp_path):
    store = StopLatchStore(tmp_path / "latch.json")
    first = DemoRuntimeGuard(latch_store=store, health_client=MockHealthClient())
    await first.inject_attempt(
        DemoInjectionRequest(fault_type="service_pause", duration_seconds=90),
        "tester",
    )
    second = DemoRuntimeGuard(latch_store=store, health_client=MockHealthClient())
    assert second.latched is True
    result = await second.inject_attempt(
        DemoInjectionRequest(fault_type="service_pause", duration_seconds=90),
        "tester",
    )
    assert result.rule_id == "STOP_LATCHED"


def test_malformed_request_rejected(demo_settings):
    from starlette.testclient import TestClient
    from app.main import create_app

    with TestClient(create_app(settings=demo_settings)) as client:
        response = client.post(
            "/api/v1/demo/injection",
            headers=auth_headers(),
            json={"fault_type": "service_pause", "duration_seconds": 90, "target_id": "wrong-target"},
        )
    assert response.status_code == 422


def test_unauthorized_reset_rejected(demo_settings):
    from starlette.testclient import TestClient
    from app.main import create_app

    with TestClient(create_app(settings=demo_settings)) as client:
        response = client.post("/api/v1/safety/reset", json={})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_ui_response_contract_fields(tmp_path):
    runtime = guard(tmp_path, health_client=MockHealthClient())
    result = await runtime.inject_attempt(
        DemoInjectionRequest(fault_type="service_pause", duration_seconds=90),
        "tester",
    )
    assert result.evidence_label == "LOCAL DEMO"
    assert result.popup_heading == "Error has occurred"
    assert result.popup_body == "Injection blocked before execution"
    assert result.target_status == "healthy"
    assert "recovery" not in result.reason.lower() or "before dispatch" in result.reason.lower()
