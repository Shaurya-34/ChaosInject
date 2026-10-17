import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class StopLatchState:
    latched: bool = False
    request_id: str | None = None
    latched_at: str | None = None


class StopLatchStore:
    """Persist STOP latch across API restarts for the local demo."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> StopLatchState:
        if not self.path.is_file():
            return StopLatchState()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or type(raw.get("latched")) is not bool:
                return StopLatchState(latched=True)
            return StopLatchState(
                latched=raw["latched"],
                request_id=raw.get("request_id"),
                latched_at=raw.get("latched_at"),
            )
        except (json.JSONDecodeError, OSError, TypeError):
            return StopLatchState(latched=True)

    def save(self, *, latched: bool, request_id: str | None = None) -> None:
        payload = {
            "latched": latched,
            "request_id": request_id,
            "latched_at": _utcnow_iso() if latched else None,
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)

    def clear(self) -> None:
        if self.path.is_file():
            self.path.unlink(missing_ok=True)
