"""Gautam's intelligence workstream: streaming learner,
experiment planner, evidence-grounded diagnosis and (disabled) rollback proposals.

``IntelligenceService`` implements ``app.api.ports.IntelligencePort``.
"""

from .service import IntelligenceService

__all__ = ["IntelligenceService"]
