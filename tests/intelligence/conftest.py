import pytest

from app.intelligence import fakes
from app.intelligence.learner import StreamingLearner
from app.intelligence.service import IntelligenceService


@pytest.fixture
def allow():
    return fakes.lab_allowlist()


@pytest.fixture
def estimator(allow):
    return fakes.FakePlanEstimator(allow)


@pytest.fixture
def svc(allow):
    # Fixture state is labelled source="fixture"; a fixture learner keeps it out of any live learner.
    return IntelligenceService(allow, learner=StreamingLearner(source="fixture"))
