"""Import shim for shared contract models.

The authoritative Pydantic definitions live in ``backend/app/contracts/``
(Sachin, contracts/v1). Until that module exists, telemetry falls back to its
provisional mirror in ``models.py``.

Everything inside the telemetry package imports contract types *from here*, so
the migration is a one-line change with no edits scattered across modules. When
``backend.app.contracts`` exports these names, this shim picks them up
automatically and ``models.py`` can be deleted.

``USING_AUTHORITATIVE_CONTRACTS`` is asserted by a test, so the day the real
contracts land we find out immediately whether the shapes still match rather
than discovering a silent divergence during integration.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_REQUIRED = (
    "CleanupStatus",
    "DataQuality",
    "EvidenceRef",
    "LoadProfile",
    "OperationAggregate",
    "Outcome",
    "Phase",
    "PhaseSummary",
    "ReadyState",
    "RecoveryStatus",
    "ResilienceScore",
    "RunSummary",
    "ScoreComponent",
    "Source",
    "SystemStateSnapshot",
    "TimelineMarker",
    "new_id",
)

USING_AUTHORITATIVE_CONTRACTS = False

try:  # pragma: no cover - exercised once contracts/v1 lands
    from backend.app import contracts as _authoritative  # type: ignore

    _missing = [name for name in _REQUIRED if not hasattr(_authoritative, name)]
    if _missing:
        raise ImportError(
            "backend.app.contracts is present but missing: " + ", ".join(_missing)
        )

    globals().update({name: getattr(_authoritative, name) for name in _REQUIRED})
    USING_AUTHORITATIVE_CONTRACTS = True
    logger.info("telemetry: using authoritative contracts from backend.app.contracts")

except ImportError:
    from .models import (  # noqa: F401
        CleanupStatus,
        DataQuality,
        EvidenceRef,
        LoadProfile,
        OperationAggregate,
        Outcome,
        Phase,
        PhaseSummary,
        ReadyState,
        RecoveryStatus,
        ResilienceScore,
        RunSummary,
        ScoreComponent,
        Source,
        SystemStateSnapshot,
        TimelineMarker,
        new_id,
    )

    logger.debug(
        "telemetry: backend.app.contracts not available; using provisional models"
    )

__all__ = list(_REQUIRED) + ["USING_AUTHORITATIVE_CONTRACTS"]
