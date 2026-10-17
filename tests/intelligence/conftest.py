import pytest

from app.intelligence.learner import StreamingLearner
from app.intelligence.service import IntelligenceService


@pytest.fixture
def svc():
    # Fixture state is labelled source="fixture"; a fixture learner keeps it out of any live learner.
    return IntelligenceService(learner=StreamingLearner(source="fixture"))
