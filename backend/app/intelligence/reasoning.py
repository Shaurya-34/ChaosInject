"""The language-model provider the two agents use to reword their explanation text.

The model's only power is to attach explanation text to findings and proposals the rules already produced; the
agents check what it returns (``llm_transport``). Nothing here can rank, score, start or stop anything.

Modes (``INTELLIGENCE_REASONING_MODE``):
  deterministic (default) - no model call
  mock                    - canned responses, for tests only
  live                    - a configured transport; requires an explicit model name
                            (``INTELLIGENCE_LIVE_MODEL``). No provider, quota or spend is assumed here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Literal, Protocol


Transport = Callable[[str, str, int, float], str]  # (system, user, max_tokens, timeout_s) -> text


class ReasoningProvider(Protocol):
    mode: Literal["deterministic", "mock", "live"]
    model_id: str | None

    def complete(self, system: str, user: str, max_tokens: int, timeout_s: float) -> str: ...


@dataclass
class DeterministicProvider:
    mode: Literal["deterministic"] = "deterministic"
    model_id: str | None = None

    def complete(self, system: str, user: str, max_tokens: int, timeout_s: float) -> str:
        raise RuntimeError("deterministic provider performs no model calls")


class MockProvider:
    """Returns queued responses (strings or exceptions). Test/fixture use only."""

    mode: Literal["mock"] = "mock"

    def __init__(self, responses: list[str | BaseException], model_id: str = "mock-reasoner"):
        self._responses = list(responses)
        self.model_id = model_id
        self.calls = 0
        self.last_user: str | None = None

    def complete(self, system: str, user: str, max_tokens: int, timeout_s: float) -> str:
        self.calls += 1
        self.last_user = user
        if not self._responses:
            raise RuntimeError("mock provider exhausted")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@dataclass
class LiveProvider:
    transport: Transport
    model_id: str
    mode: Literal["live"] = "live"
    location: str | None = None  # "local" (loopback, e.g. Ollama) or "hosted"; None when unknown

    def complete(self, system: str, user: str, max_tokens: int, timeout_s: float) -> str:
        return self.transport(system, user, max_tokens, timeout_s)


@dataclass(frozen=True)
class ReasoningBudget:
    max_attempts: int = 2
    timeout_seconds: float = 20.0
    max_output_tokens: int = 800
    max_explanation_chars: int = 400


def provider_from_env(transport: Transport | None = None) -> tuple[ReasoningProvider, list[str]]:
    """Build the configured provider. Falls back to deterministic with a stated limitation."""

    mode = os.environ.get("INTELLIGENCE_REASONING_MODE", "deterministic").strip().lower()
    if mode == "live":
        model = os.environ.get("INTELLIGENCE_LIVE_MODEL", "").strip()
        if not model:
            return DeterministicProvider(), ["Live reasoning requested but INTELLIGENCE_LIVE_MODEL is not set; rule-based explanations are shown."]
        location = None
        if transport is None:
            from .llm_transport import transport_from_env  # lazy: only the live path needs httpx

            transport, location, notes = transport_from_env()
            if transport is None:
                return DeterministicProvider(), notes
        return LiveProvider(transport=transport, model_id=model, location=location), []
    if mode == "mock":
        return MockProvider([]), ["Mock reasoning provider configured; outputs are fixtures, not model reasoning."]
    return DeterministicProvider(), []
