from dataclasses import dataclass
import os
import secrets

LOCAL_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")


@dataclass
class Settings:
    operator_token: str = ""
    origins: tuple[str, ...] = LOCAL_ORIGINS + ("http://127.0.0.1:8000", "http://localhost:8000")
    chaos_execution_enabled: bool = False

    def __post_init__(self):
        if not self.operator_token: self.operator_token = secrets.token_urlsafe(32)
        if len(self.operator_token) < 32: raise ValueError("Operator token needs at least 32 characters")

    @classmethod
    def environment(cls):
        port = int(os.getenv("LOCAL_API_PORT", "8000"))
        return cls(operator_token=os.getenv("OPERATOR_TOKEN", ""), origins=LOCAL_ORIGINS + (f"http://127.0.0.1:{port}", f"http://localhost:{port}"), chaos_execution_enabled=os.getenv("CHAOS_EXECUTION_ENABLED", "false").lower() == "true")
