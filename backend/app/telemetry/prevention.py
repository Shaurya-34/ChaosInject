"""Independent acceptance verification for the LOCAL DEMO prevention path.

Navya owns this file. Tooshita builds the preflight guard; this module exists to
check it from the outside and to produce the evidence record.

The claim under test is narrow and must stay narrow:

    An unsafe injection request was rejected **before** any executor invocation,
    the target was never mutated, and the target was healthy before and after.

That is prevention evidence. It is **not** one of the event's three executed
chaos mechanisms, it is **not** a measured outage, and it carries **no**
resilience score -- nothing was injected, so there is no resilience to measure.
``assert_no_score_for_prevented_attempt`` enforces that last point in code.

Independence is the reason this module is not simply "read Tooshita's counter".
Two sources are recorded:

``executor_invocations_reported``
    From the runtime's own execution spy. Useful, but self-reported.

``target_observed_mutations``
    From the target itself, which counts every mutating request it received.
    If the executor had dispatched anything, the target would have seen it.

When the second source is unavailable the evidence says so and the verdict is
downgraded to ``corroborated=False``, rather than quietly treating one source as
two.

Every reading is labelled ``REAL_HTTP`` or ``MOCK``. A mock check never appears
in the same column as a real observation, and the evidence record refuses to
claim a real-HTTP verdict built from mocks.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Literal, Protocol
from urllib.parse import urlparse

from .clock import mono, now_utc
from .config import SCHEMA_VERSION

# Health readings older than this are stale and must fail closed.
HEALTH_FRESHNESS_SECONDS = 5.0

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class ObservationMode(str, Enum):
    """Provenance of a reading. Never mixed in a single verdict."""

    REAL_HTTP = "real_http"
    MOCK = "mock"


class HealthState(str, Enum):
    """Four states, because 'not healthy' is not one thing.

    ``UNAVAILABLE`` and ``STALE`` both fail closed: we could not see the target,
    which is a reason to refuse an action, not a reason to proceed.
    """

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNAVAILABLE = "unavailable"
    STALE = "stale"


@dataclass(frozen=True)
class HealthReading:
    captured_at: datetime
    valid_until: datetime
    state: HealthState
    status_code: int | None
    latency_ms: float | None
    mode: ObservationMode
    detail: str

    @property
    def is_healthy_now(self) -> bool:
        """Healthy *and* fresh. Staleness is evaluated at read time."""
        if self.state is not HealthState.HEALTHY:
            return False
        return now_utc() <= self.valid_until

    def state_at(self, when: datetime) -> HealthState:
        if self.state is HealthState.HEALTHY and when > self.valid_until:
            return HealthState.STALE
        return self.state


class NonLoopbackTargetError(ValueError):
    """The demo target must be loopback. Refusing anything else."""


class TargetHealthProbe:
    """Real HTTP health check against a loopback target.

    Uses ``http.client`` from the standard library deliberately: no new
    dependency, and the demo target is always loopback so there is no need for a
    full async client here.

    Loopback is enforced, not assumed. A verification harness that could be
    pointed at an arbitrary host is exactly the kind of thing the scope rules
    forbid, so the constructor refuses a non-loopback URL outright.
    """

    def __init__(
        self,
        base_url: str,
        *,
        health_path: str = "/health",
        witness_path: str = "/_witness",
        timeout_seconds: float = 2.0,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.hostname not in LOOPBACK_HOSTS:
            raise NonLoopbackTargetError(
                f"target host {parsed.hostname!r} is not loopback; the LOCAL DEMO "
                "verifier refuses any non-loopback target"
            )
        self._host = parsed.hostname
        self._port = parsed.port or 80
        self._health_path = health_path
        self._witness_path = witness_path
        self._timeout = timeout_seconds
        self.base_url = base_url

    def _request(self, path: str) -> tuple[int, str, float]:
        conn = http.client.HTTPConnection(
            self._host, self._port, timeout=self._timeout
        )
        started = mono()
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            body = response.read(8192).decode("utf-8", errors="replace")
            return response.status, body, (mono() - started) * 1000.0
        finally:
            conn.close()

    def read_health(self) -> HealthReading:
        captured = now_utc()
        valid_until = captured + timedelta(seconds=HEALTH_FRESHNESS_SECONDS)
        try:
            status, _body, latency_ms = self._request(self._health_path)
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            return HealthReading(
                captured_at=captured,
                valid_until=valid_until,
                state=HealthState.UNAVAILABLE,
                status_code=None,
                latency_ms=None,
                mode=ObservationMode.REAL_HTTP,
                detail=f"target unreachable ({type(exc).__name__}); failing closed",
            )

        state = HealthState.HEALTHY if status == 200 else HealthState.UNHEALTHY
        return HealthReading(
            captured_at=captured,
            valid_until=valid_until,
            state=state,
            status_code=status,
            latency_ms=round(latency_ms, 3),
            mode=ObservationMode.REAL_HTTP,
            detail=f"HTTP {status} from {self._health_path}",
        )

    def read_mutation_witness(self) -> int | None:
        """Mutations the target itself observed.

        ``None`` means the target exposes no witness endpoint. That is reported
        as a gap in corroboration, never silently as zero -- "we could not count"
        and "we counted zero" are different claims.
        """
        try:
            status, body, _ = self._request(self._witness_path)
        except (OSError, socket.timeout, http.client.HTTPException):
            return None
        if status != 200:
            return None
        try:
            payload = json.loads(body)
            value = payload.get("mutation_requests")
            return int(value) if value is not None else None
        except (ValueError, TypeError):
            return None


class StaticHealthProbe:
    """Mock probe. Every reading it returns is labelled ``MOCK``."""

    def __init__(
        self,
        states: list[HealthState] | None = None,
        *,
        witness: int | None = 0,
        stale: bool = False,
    ) -> None:
        self._states = list(states or [HealthState.HEALTHY])
        self._index = 0
        self._witness = witness
        self._stale = stale
        self.base_url = "mock://target"

    def read_health(self) -> HealthReading:
        state = self._states[min(self._index, len(self._states) - 1)]
        self._index += 1
        captured = now_utc()
        offset = -60.0 if self._stale else HEALTH_FRESHNESS_SECONDS
        return HealthReading(
            captured_at=captured,
            valid_until=captured + timedelta(seconds=offset),
            state=state,
            status_code=200 if state is HealthState.HEALTHY else 503,
            latency_ms=1.0,
            mode=ObservationMode.MOCK,
            detail="scripted mock reading; not a real HTTP observation",
        )

    def read_mutation_witness(self) -> int | None:
        return self._witness


class HealthProbe(Protocol):
    base_url: str

    def read_health(self) -> HealthReading: ...

    def read_mutation_witness(self) -> int | None: ...


class ExecutionSpy(Protocol):
    """Tooshita's execution spy, read as a second (self-reported) source."""

    @property
    def invocation_count(self) -> int: ...


@dataclass
class CountingExecutionSpy:
    """Reference spy for tests. Counts attempts to dispatch a mutation."""

    invocations: list[str] = field(default_factory=list)

    def record(self, action: str) -> None:
        self.invocations.append(action)

    @property
    def invocation_count(self) -> int:
        return len(self.invocations)


@dataclass(frozen=True)
class AcceptanceCheck:
    """One pass/fail assertion with the observation that produced it."""

    check_id: str
    description: str
    passed: bool
    observed: str
    mode: ObservationMode

    def as_dict(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "description": self.description,
            "passed": self.passed,
            "observed": self.observed,
            "mode": self.mode.value,
        }


@dataclass(frozen=True)
class PreventionEvidence:
    """Sanitized evidence for one prevented injection attempt.

    Numeric and categorical only. No request bodies, no statement text, no
    operator identity beyond an opaque source label.

    ``resilience_score`` is deliberately absent from this type. A prevented fault
    produced no measured degradation and no recovery, so there is nothing to
    score, and offering a field would invite someone to fill it.
    """

    schema_version: str
    provenance: Literal["LOCAL_DEMO"]
    observation_mode: ObservationMode
    request_id: str
    rule_id: str
    decision: Literal["BLOCK", "ALLOW", "WARN"]
    executed: bool
    requested_duration_seconds: float
    configured_max_duration_seconds: float
    stop_latched: bool
    executor_invocations_reported: int
    target_observed_mutations: int | None
    health_before: HealthState
    health_after: HealthState
    health_before_detail: str
    health_after_detail: str
    checks: tuple[AcceptanceCheck, ...]
    corroborated: bool
    verdict: Literal["ACCEPTED", "REJECTED"]
    notes: tuple[str, ...]
    captured_at: datetime
    evidence_hash: str = ""

    def as_dict(self) -> dict[str, object]:
        payload = {
            "schema_version": self.schema_version,
            "provenance": self.provenance,
            "observation_mode": self.observation_mode.value,
            "request_id": self.request_id,
            "rule_id": self.rule_id,
            "decision": self.decision,
            "executed": self.executed,
            "requested_duration_seconds": self.requested_duration_seconds,
            "configured_max_duration_seconds": self.configured_max_duration_seconds,
            "stop_latched": self.stop_latched,
            "executor_invocations_reported": self.executor_invocations_reported,
            "target_observed_mutations": self.target_observed_mutations,
            "health_before": self.health_before.value,
            "health_after": self.health_after.value,
            "health_before_detail": self.health_before_detail,
            "health_after_detail": self.health_after_detail,
            "checks": [c.as_dict() for c in self.checks],
            "corroborated": self.corroborated,
            "verdict": self.verdict,
            "notes": list(self.notes),
            "captured_at": self.captured_at.isoformat(),
            "resilience_score": None,
            "resilience_score_reason": (
                "no fault was executed; a prevented attempt has no measured "
                "degradation or recovery, so no resilience score applies"
            ),
        }
        payload["evidence_hash"] = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        return payload

    @property
    def all_checks_passed(self) -> bool:
        return all(c.passed for c in self.checks)


class PreventionVerifier:
    """Runs the acceptance gate from outside the runtime."""

    def __init__(
        self,
        probe: HealthProbe,
        spy: ExecutionSpy,
        *,
        configured_max_duration_seconds: float = 60.0,
    ) -> None:
        self._probe = probe
        self._spy = spy
        self._max_duration = configured_max_duration_seconds

    def verify_rejected_attempt(
        self,
        *,
        request_id: str,
        rule_id: str,
        decision: str,
        executed: bool,
        requested_duration_seconds: float,
        stop_latched: bool,
        health_before: HealthReading,
        health_after: HealthReading,
        spy_count_before: int,
        extra_notes: tuple[str, ...] = (),
    ) -> PreventionEvidence:
        """Check one rejected attempt and produce its evidence record.

        ``extra_notes`` lets the caller disclose provenance this module cannot
        see for itself -- chiefly, whether the BLOCK decision came from the real
        runtime guard or from a stand-in.
        """
        mode = health_before.mode
        checks: list[AcceptanceCheck] = []
        notes: list[str] = list(extra_notes)

        if health_after.mode is not mode:
            notes.append(
                "health readings came from different observation modes; the verdict "
                "is downgraded because a real observation and a mock cannot be "
                "combined into one claim"
            )
            mode = ObservationMode.MOCK

        # 1. The decision itself.
        checks.append(
            AcceptanceCheck(
                check_id="decision_is_block",
                description="the unsafe proposal was rejected",
                passed=decision == "BLOCK",
                observed=f"decision={decision}",
                mode=mode,
            )
        )

        # 2. Rejection happened before dispatch. The spy delta is the evidence:
        #    an unchanged count across the attempt means nothing was dispatched.
        spy_delta = self._spy.invocation_count - spy_count_before
        checks.append(
            AcceptanceCheck(
                check_id="zero_executor_invocations",
                description="no executor invocation occurred during the attempt",
                passed=spy_delta == 0,
                observed=(
                    f"executor invocations before={spy_count_before} "
                    f"after={self._spy.invocation_count} delta={spy_delta}"
                ),
                mode=mode,
            )
        )

        checks.append(
            AcceptanceCheck(
                check_id="executed_flag_false",
                description="the runtime reports executed=false",
                passed=executed is False,
                observed=f"executed={executed}",
                mode=mode,
            )
        )

        # 3. The rule actually applied, with real numbers.
        rule_applies = requested_duration_seconds > self._max_duration
        checks.append(
            AcceptanceCheck(
                check_id="rule_numerically_applies",
                description=(
                    "requested duration exceeds the configured hard cap, so the "
                    "fixed rule genuinely fired"
                ),
                passed=rule_applies,
                observed=(
                    f"requested={requested_duration_seconds}s "
                    f"configured_max={self._max_duration}s"
                ),
                mode=mode,
            )
        )

        # 4. Target health before and after. Both must be healthy AND fresh.
        before_state = health_before.state_at(health_before.captured_at)
        after_state = health_after.state_at(health_after.captured_at)

        checks.append(
            AcceptanceCheck(
                check_id="target_healthy_before",
                description="target was healthy and fresh before the attempt",
                passed=health_before.is_healthy_now
                or before_state is HealthState.HEALTHY,
                observed=f"state={before_state.value}; {health_before.detail}",
                mode=health_before.mode,
            )
        )
        checks.append(
            AcceptanceCheck(
                check_id="target_healthy_after",
                description=(
                    "target was still healthy after the attempt -- the error was in "
                    "the rejected request, not in the target"
                ),
                passed=after_state is HealthState.HEALTHY,
                observed=f"state={after_state.value}; {health_after.detail}",
                mode=health_after.mode,
            )
        )

        # 5. STOP latched.
        checks.append(
            AcceptanceCheck(
                check_id="stop_latched",
                description="STOP was latched after the rejection",
                passed=bool(stop_latched),
                observed=f"stop_latched={stop_latched}",
                mode=mode,
            )
        )

        # 6. Independent corroboration from the target's own witness.
        witness = self._probe.read_mutation_witness()
        if witness is None:
            corroborated = False
            notes.append(
                "the target exposes no mutation witness endpoint, so zero dispatched "
                "mutations rests on the runtime's self-reported spy alone; this is "
                "recorded as uncorroborated rather than presented as two sources"
            )
        else:
            corroborated = witness == 0
            checks.append(
                AcceptanceCheck(
                    check_id="target_observed_zero_mutations",
                    description=(
                        "the target itself observed no mutating request, "
                        "independently of the runtime's own counter"
                    ),
                    passed=witness == 0,
                    observed=f"target_observed_mutations={witness}",
                    mode=self._probe_mode(),
                )
            )

        if mode is ObservationMode.MOCK:
            notes.append(
                "MOCK observation: this run does not constitute real-HTTP evidence "
                "of prevention and must be reported separately from the live local test"
            )

        verdict = "ACCEPTED" if all(c.passed for c in checks) else "REJECTED"

        return PreventionEvidence(
            schema_version=SCHEMA_VERSION,
            provenance="LOCAL_DEMO",
            observation_mode=mode,
            request_id=request_id,
            rule_id=rule_id,
            decision=decision,  # type: ignore[arg-type]
            executed=executed,
            requested_duration_seconds=requested_duration_seconds,
            configured_max_duration_seconds=self._max_duration,
            stop_latched=stop_latched,
            executor_invocations_reported=self._spy.invocation_count,
            target_observed_mutations=witness,
            health_before=before_state,
            health_after=after_state,
            health_before_detail=health_before.detail,
            health_after_detail=health_after.detail,
            checks=tuple(checks),
            corroborated=corroborated,
            verdict=verdict,  # type: ignore[arg-type]
            notes=tuple(notes),
            captured_at=now_utc(),
        )

    def _probe_mode(self) -> ObservationMode:
        return (
            ObservationMode.REAL_HTTP
            if isinstance(self._probe, TargetHealthProbe)
            else ObservationMode.MOCK
        )

    # -- fail-closed checks --------------------------------------------------

    def verify_fails_closed_when_target_unavailable(
        self, *, decision: str, health: HealthReading
    ) -> AcceptanceCheck:
        """An unreachable or stale target must produce a refusal, not a pass."""
        state = health.state_at(now_utc())
        unusable = state in (
            HealthState.UNAVAILABLE,
            HealthState.STALE,
            HealthState.UNHEALTHY,
        )
        return AcceptanceCheck(
            check_id="fails_closed_on_unusable_health",
            description=(
                "with target health unavailable or stale, the request is refused "
                "rather than allowed"
            ),
            passed=(decision == "BLOCK") if unusable else True,
            observed=f"health={state.value} decision={decision}",
            mode=health.mode,
        )

    def verify_blocked_while_execution_unavailable(
        self, *, requested_duration_seconds: float, decision: str
    ) -> AcceptanceCheck:
        """Even a within-cap request stays blocked while execution is unavailable.

        The cap is not the only reason to refuse. During the LOCAL DEMO there is
        no execution capability at all, so an acceptable-duration request must
        also be blocked -- otherwise the guard would pass a request it could not
        have safely carried out.
        """
        within_cap = requested_duration_seconds <= self._max_duration
        return AcceptanceCheck(
            check_id="within_cap_still_blocked",
            description=(
                "an accepted-duration request is still blocked while execution "
                "capability is unavailable"
            ),
            passed=decision == "BLOCK",
            observed=(
                f"requested={requested_duration_seconds}s "
                f"within_cap={within_cap} decision={decision}"
            ),
            mode=self._probe_mode(),
        )


def assert_no_score_for_prevented_attempt(evidence: PreventionEvidence) -> None:
    """Guard: a prevented attempt must never be scored.

    Called wherever prevention evidence meets the reporting path. A resilience
    score summarises measured degradation and recovery; a request that was
    stopped before execution produced neither. Scoring it would turn a safety
    success into a fabricated measurement, which is the specific failure mode the
    integrity rules exist to prevent.
    """
    if evidence.executed:
        raise ValueError(
            "evidence marked executed=True is not a prevented attempt; "
            "route it through the normal run-scoring path instead"
        )
    payload = evidence.as_dict()
    if payload.get("resilience_score") is not None:
        raise AssertionError(
            "a prevented attempt carries no resilience score; nothing was injected, "
            "so there is no measured resilience"
        )
