import pytest

from app.intelligence.service import IntelligenceService


@pytest.fixture
def svc():
    return IntelligenceService()
