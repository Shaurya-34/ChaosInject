"""Tooshita runtime: deterministic preflight, STOP latch, and demo safety."""

from .constants import MAX_FAULT_DURATION_SECONDS
from .demo_contract import DemoInjectionRequest, DemoInjectionResult
from .demo_guard import DemoRuntimeGuard

__all__ = [
    "MAX_FAULT_DURATION_SECONDS",
    "DemoInjectionRequest",
    "DemoInjectionResult",
    "DemoRuntimeGuard",
]
