from typing import Annotated, Literal
from pydantic import AwareDatetime, Field, model_validator
from app.contracts import Contract, Observed, Count
from .constants import EVIDENCE_LABEL, MAX_FAULT_DURATION_SECONDS, POPUP_BODY, POPUP_HEADING


class DemoInjectionRequest(Contract):
    fault_type: Literal["service_pause"]
    target_id: Literal["demo-target"] = "demo-target"
    duration_seconds: Annotated[int, Field(ge=1, le=600, strict=True)]


class DemoInjectionResult(Observed):
    request_id: str
    captured_at: AwareDatetime
    decision: Literal["ALLOW", "BLOCK"]
    evidence_label: Literal["LOCAL DEMO"] = EVIDENCE_LABEL
    popup_heading: Literal["Error has occurred", "Safety check passed"] = POPUP_HEADING
    popup_body: Literal["Injection blocked before execution", "Duration accepted for demo"] = POPUP_BODY
    message: str
    reason: str
    rule_id: str
    stop_latched: bool = True
    executed: Literal[False] = False
    target_status: Literal["healthy", "unavailable"]
    target_requests: Count
    mutations_dispatched: Literal[0] = 0
    duration_seconds: Count
    maximum_seconds: Literal[60] = MAX_FAULT_DURATION_SECONDS

    @model_validator(mode="after")
    def consistent_result(self):
        if self.decision == "ALLOW":
            if self.stop_latched or self.target_status != "healthy" or not 1 <= self.duration_seconds <= self.maximum_seconds or self.rule_id != "DURATION_WITHIN_LIMIT" or self.popup_heading != "Safety check passed" or self.popup_body != "Duration accepted for demo":
                raise ValueError("Success must describe a healthy, permitted duration check")
        elif not self.stop_latched or self.popup_heading != POPUP_HEADING or self.popup_body != POPUP_BODY:
            raise ValueError("Blocked attempts require STOP and error messaging")
        return self


class DemoSafetyStatus(Observed):
    stop_latched: bool
    last_result: DemoInjectionResult | None
