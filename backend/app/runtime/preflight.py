from dataclasses import dataclass
from .constants import LOCAL_DEMO_TARGET_ID, MAX_FAULT_DURATION_SECONDS
from .demo_contract import DemoInjectionRequest


@dataclass(frozen=True)
class PreflightOutcome:
    rule_id: str
    reason: str
    passed: bool = False


def evaluate_preflight(
    *,
    body: DemoInjectionRequest,
    latched: bool,
    target_status: str,
    maximum_seconds: int = MAX_FAULT_DURATION_SECONDS,
) -> PreflightOutcome:
    if body.target_id != LOCAL_DEMO_TARGET_ID:
        return PreflightOutcome(
            "INVALID_TARGET",
            f"Target {body.target_id!r} is not enrolled for the local demo.",
        )
    if latched:
        return PreflightOutcome(
            "STOP_LATCHED",
            "STOP is already latched. Reset after confirming target health.",
        )
    if target_status != "healthy":
        return PreflightOutcome(
            "TARGET_UNAVAILABLE",
            "Required target health is unavailable. Execution was stopped before dispatch.",
        )
    if body.duration_seconds > maximum_seconds:
        return PreflightOutcome(
            "DURATION_HARD_LIMIT",
            f"Requested outage is {body.duration_seconds} seconds, exceeding the fixed {maximum_seconds}-second maximum. Execution was stopped before dispatch.",
        )
    return PreflightOutcome(
        "DURATION_WITHIN_LIMIT",
        f"Requested duration is {body.duration_seconds} seconds, within the {maximum_seconds}-second limit. Safety check passed; no outage was executed.",
        passed=True,
    )
