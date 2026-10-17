import pytest

from app.intelligence.service import IntelligenceService


@pytest.fixture
def svc():
    return IntelligenceService()


@pytest.fixture(autouse=True)
def _no_leftover_llm_backoff():
    """The LLM backoff is process-wide on purpose; a test's simulated 429 must not leak into the next test."""
    from app.intelligence import llm_transport

    llm_transport.reset_backoff()
    yield
    llm_transport.reset_backoff()
