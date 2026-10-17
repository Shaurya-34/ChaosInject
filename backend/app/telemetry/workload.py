"""Synthetic workload: operation definitions and semantic success verification.

The central rule here: **HTTP 200 is not success.** Online Boutique's frontend
renders a styled error page with a 200 status in several failure modes, and a
cart page that loads but shows no items is a failed add-to-cart. Counting those
as successes would make a Redis outage look like a latency blip, which is
exactly the finding we are trying to measure.

So every operation declares a ``success_marker`` that must appear in the
response, and a set of ``failure_markers`` that must not. Those strings are
properties of the pinned upstream revision, not of this code, so they live in a
calibration file that is produced by running ``calibrate`` against the real
application and committed alongside the run.

``WorkloadCalibration.verified`` is ``False`` until that happens, and
``WorkloadDefinition.build`` refuses to produce a strict workload from an
unverified calibration. An uncalibrated probe would silently report whatever the
default guesses happened to match, which is worse than refusing to start.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from ._contracts import Outcome
from .transport import (
    HttpTransport,
    ProbeResponse,
    TransportConnectionError,
    TransportTimeout,
)

Method = Literal["GET", "POST"]


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """One synthetic operation against the application under test."""

    name: str
    method: Method
    path_template: str
    success_marker: str
    failure_markers: tuple[str, ...] = ()
    form_template: dict[str, str] = field(default_factory=dict)
    critical: bool = False

    def render_path(self, context: dict[str, str]) -> str:
        return self.path_template.format(**context)

    def render_form(self, context: dict[str, str]) -> dict[str, str]:
        return {k: v.format(**context) for k, v in self.form_template.items()}


@dataclass
class WorkloadCalibration:
    """Markers and identifiers measured against the pinned application.

    ``verified`` flips to ``True`` only when a human has run the calibration
    against the real deployment and confirmed each marker discriminates success
    from failure. The defaults below are starting guesses for Online Boutique and
    are explicitly **not** evidence of anything.
    """

    upstream_sha: str | None = None
    calibrated_at: str | None = None
    calibrated_by: str | None = None
    verified: bool = False
    base_url: str = "http://127.0.0.1:8080"
    product_ids: tuple[str, ...] = ()
    markers: dict[str, str] = field(default_factory=dict)
    failure_markers: tuple[str, ...] = ("Error", "error-page", "Internal Server Error")
    checkout_form: dict[str, str] = field(default_factory=dict)
    add_to_cart_form: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @classmethod
    def unverified_default(cls) -> "WorkloadCalibration":
        """Starting point for calibration. Not usable for a reported run.

        Every value here is a hypothesis about upstream Online Boutique that must
        be confirmed before any measurement is published.
        """
        return cls(
            verified=False,
            product_ids=(),
            markers={
                "browse": "TBD -- calibrate: marker proving the product grid rendered",
                "product_view": "TBD -- calibrate: marker proving a product detail rendered",
                "add_to_cart": "TBD -- calibrate: marker proving the item reached the cart",
                "cart_read": "TBD -- calibrate: marker proving the cart page rendered",
                "checkout": "TBD -- calibrate: marker proving the order confirmation rendered",
            },
            add_to_cart_form={
                "product_id": "{product_id}",
                "quantity": "1",
            },
            checkout_form={},
            notes=[
                "Run scripts/telemetry/calibrate_markers.py against the pinned "
                "deployment, confirm each marker appears on success and is absent "
                "on a forced failure, then commit the result.",
                "Field names for add_to_cart / checkout forms must be read from the "
                "pinned frontend templates, not assumed.",
            ],
        )

    @classmethod
    def load(cls, path: str | Path) -> "WorkloadCalibration":
        data = json.loads(Path(path).read_text())
        data["product_ids"] = tuple(data.get("product_ids", ()))
        data["failure_markers"] = tuple(data.get("failure_markers", ()))
        return cls(**data)

    def save(self, path: str | Path) -> None:
        payload = asdict(self)
        payload["product_ids"] = list(self.product_ids)
        payload["failure_markers"] = list(self.failure_markers)
        Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True))

    def unresolved_markers(self) -> list[str]:
        return [
            name
            for name, marker in self.markers.items()
            if not marker or marker.startswith("TBD")
        ]


class UncalibratedWorkloadError(RuntimeError):
    """Raised when a strict workload is built from unverified calibration."""


@dataclass(frozen=True, slots=True)
class OperationResult:
    """Numeric-only outcome of one attempt.

    Deliberately carries no body, no cookie, no URL and no exception text. This
    is the only thing that leaves the probe.
    """

    operation: str
    outcome: Outcome
    duration_ms: float | None
    error_category: str | None = None


class WorkloadDefinition:
    """The set of operations the probe will drive."""

    def __init__(
        self,
        calibration: WorkloadCalibration,
        operations: dict[str, OperationSpec],
    ) -> None:
        self.calibration = calibration
        self.operations = operations

    @classmethod
    def build(
        cls, calibration: WorkloadCalibration, *, strict: bool = True
    ) -> "WorkloadDefinition":
        """Construct operations from calibration.

        ``strict=False`` is for unit tests and dry runs only. Any workload used
        for a reported measurement must be strict.
        """
        if strict:
            if not calibration.verified:
                raise UncalibratedWorkloadError(
                    "workload calibration is not verified against the pinned "
                    "application; run the calibration script before measuring"
                )
            unresolved = calibration.unresolved_markers()
            if unresolved:
                raise UncalibratedWorkloadError(
                    "unresolved success markers: " + ", ".join(sorted(unresolved))
                )
            if not calibration.product_ids:
                raise UncalibratedWorkloadError(
                    "no product IDs captured; browse and add_to_cart cannot be driven"
                )

        markers = calibration.markers
        failures = calibration.failure_markers

        operations = {
            "browse": OperationSpec(
                name="browse",
                method="GET",
                path_template="/",
                success_marker=markers.get("browse", ""),
                failure_markers=failures,
            ),
            "product_view": OperationSpec(
                name="product_view",
                method="GET",
                path_template="/product/{product_id}",
                success_marker=markers.get("product_view", ""),
                failure_markers=failures,
            ),
            "add_to_cart": OperationSpec(
                name="add_to_cart",
                method="POST",
                path_template="/cart",
                success_marker=markers.get("add_to_cart", ""),
                failure_markers=failures,
                form_template=calibration.add_to_cart_form,
                critical=True,
            ),
            "cart_read": OperationSpec(
                name="cart_read",
                method="GET",
                path_template="/cart",
                success_marker=markers.get("cart_read", ""),
                failure_markers=failures,
                critical=True,
            ),
            "checkout": OperationSpec(
                name="checkout",
                method="POST",
                path_template="/cart/checkout",
                success_marker=markers.get("checkout", ""),
                failure_markers=failures,
                form_template=calibration.checkout_form,
                critical=True,
            ),
        }
        return cls(calibration, operations)

    def pick_context(self, rng: random.Random) -> dict[str, str]:
        ids = self.calibration.product_ids or ("UNCALIBRATED",)
        return {"product_id": rng.choice(ids)}


def classify(spec: OperationSpec, response: ProbeResponse) -> Outcome:
    """Decide whether an attempt actually succeeded.

    Order matters. A failure marker wins over a success marker, because an error
    page that happens to contain a common word should never be scored as a
    success.
    """
    if response.status_code >= 500:
        return Outcome.HTTP_ERROR
    if response.status_code >= 400:
        return Outcome.HTTP_ERROR

    head = response.body_head
    for marker in spec.failure_markers:
        if marker and marker in head:
            return Outcome.SEMANTIC_FAILURE
    if spec.success_marker and spec.success_marker not in head:
        return Outcome.SEMANTIC_FAILURE
    return Outcome.SUCCESS


async def execute(
    spec: OperationSpec,
    transport: HttpTransport,
    context: dict[str, str],
) -> OperationResult:
    """Run one operation and return numbers only.

    A timeout yields ``duration_ms=None``: the request never completed, so there
    is no latency to report, and inventing the timeout value as a duration would
    quietly compress the tail of the distribution.
    """
    path = spec.render_path(context)
    try:
        if spec.method == "GET":
            response = await transport.get(path)
        else:
            response = await transport.post(path, spec.render_form(context))
    except TransportTimeout:
        return OperationResult(
            operation=spec.name,
            outcome=Outcome.TIMEOUT,
            duration_ms=None,
            error_category="timeout",
        )
    except TransportConnectionError:
        return OperationResult(
            operation=spec.name,
            outcome=Outcome.CONNECTION_ERROR,
            duration_ms=None,
            error_category="connection",
        )

    outcome = classify(spec, response)
    category = None if outcome is Outcome.SUCCESS else outcome.value
    return OperationResult(
        operation=spec.name,
        outcome=outcome,
        duration_ms=response.elapsed_ms,
        error_category=category,
    )
