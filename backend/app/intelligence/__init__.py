"""Gautam's intelligence workstream: SQL guard, query policy, streaming learner,
experiment planner, evidence-grounded diagnosis and (disabled) rollback proposals.

``IntelligenceService`` implements ``app.api.ports.IntelligencePort``.
"""

from .service import IntelligenceService

__all__ = ["IntelligenceService"]
