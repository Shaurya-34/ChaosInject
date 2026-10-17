"""Day 2 adaptive rollback proposals: interface only, disabled on Day 1.

When enabled (Day 2), this module may *propose* reverting one approved
``stateless_replicas`` change when fresh evidence shows sustained regression.
It never authorises or applies anything: Tooshita's runtime checks opt-in,
expiry, target UID, expected field value, the mutation lock and the
one-revert budget. See docs/rollback-specification.md.
"""

from __future__ import annotations

import os

from app.contracts import ActionProposal, EvidenceRef


class RollbackDisabled(RuntimeError):
    pass


def rollback_enabled() -> bool:
    return os.environ.get("ADAPTIVE_ROLLBACK_ENABLED", "false").strip().lower() == "true"


def propose_rollback(
    *,
    sandbox_id: str,
    target_id: str,
    prior_replicas: int,
    applied_replicas: int,
    evidence_refs: list[EvidenceRef],
    observation_seconds: int,
    auto_revert_requested: bool,
) -> ActionProposal:
    if not rollback_enabled():
        raise RollbackDisabled("Adaptive rollback is a Day 2 feature; ADAPTIVE_ROLLBACK_ENABLED=false.")
    raise NotImplementedError("Day 2: implement per the shared rollback specification.")
