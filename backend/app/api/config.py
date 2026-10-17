from dataclasses import dataclass
import importlib
import os
import secrets
from pathlib import Path
from typing import Literal
from app.api.ports import Ports

@dataclass
class Settings:
    mode: Literal["fixture", "live", "replay", "demo"] = "fixture"
    operator_token: str = ""
    sandbox_id: str = "fixture-sandbox"
    context: str = "chaosinject-local"
    namespace: str = "hotel-sandbox"
    origins: tuple[str, ...] = ("http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:8000", "http://localhost:8000")
    fake_runtime_enabled: bool = False
    chaos_execution_enabled: bool = False
    def __post_init__(self):
        if self.mode not in ("fixture", "live", "replay", "demo"): raise ValueError("Unsupported data mode")
        if self.mode != "fixture" and not self.operator_token: raise ValueError("OPERATOR_TOKEN is required")
        if not self.operator_token: self.operator_token = secrets.token_urlsafe(32)
        if len(self.operator_token) < 32: raise ValueError("Operator token needs at least 32 characters")
        if self.mode != "fixture" and self.fake_runtime_enabled: raise ValueError("Fake runtime is fixture-only")
    @classmethod
    def environment(cls):
        port = int(os.getenv("LOCAL_API_PORT", "8000"))
        return cls(origins=("http://127.0.0.1:5173", "http://localhost:5173", f"http://127.0.0.1:{port}", f"http://localhost:{port}"), mode=os.getenv("DATA_MODE", "fixture"), operator_token=os.getenv("OPERATOR_TOKEN", ""), sandbox_id=os.getenv("SANDBOX_ID", "fixture-sandbox"), context=os.getenv("KUBE_CONTEXT", "chaosinject-local"), fake_runtime_enabled=os.getenv("FAKE_RUNTIME_ENABLED", "false").lower() == "true", chaos_execution_enabled=os.getenv("CHAOS_EXECUTION_ENABLED", "false").lower() == "true")

def configured_ports(settings):
    if settings.mode == "demo":
        from app.adapters.demo import demo_ports
        settings.context = "local-demo"
        settings.namespace = "local-http"
        settings.sandbox_id = "local-demo"
        return demo_ports(settings)
    if settings.mode == "fixture":
        from app.adapters.fixtures import fixture_ports
        return fixture_ports(settings.fake_runtime_enabled, delay=0.8)
    if settings.mode == "replay":
        from app.adapters.replay import replay_ports
        if not os.getenv("REPLAY_BUNDLE"): raise ValueError("Replay requires an explicit recorded bundle")
        return replay_ports(Path(os.environ["REPLAY_BUNDLE"]))
    factory = os.getenv("LIVE_PORTS_FACTORY")
    if not factory or ":" not in factory: raise ValueError("Live needs LIVE_PORTS_FACTORY=module:function; no fixture fallback")
    module, function = factory.split(":", 1)
    ports = getattr(importlib.import_module(module), function)(settings)
    if not isinstance(ports, Ports) or not ports.runtime_safety_ready: raise ValueError("Live runtime must attest independent watchdog/reconciliation readiness")
    for obj, names in [(ports.platform, ["get_topology", "get_target"]), (ports.telemetry, ["get_state", "begin_phase", "close_phase", "finalize_run"]), (ports.intelligence, ["plan", "analyze_run"]), (ports.runtime, ["catalog", "validate", "approve", "start", "status", "events", "stop", "emergency_stop", "reconcile", "reset"])]:
        if not all(callable(getattr(obj, method, None)) for method in names): raise ValueError("Incomplete live port implementation")
    return ports
