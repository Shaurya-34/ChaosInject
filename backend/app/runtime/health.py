from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import httpx
from app.api.errors import PortError
from .constants import HEALTH_FRESH_SECONDS, LOCAL_DEMO_TARGET_HEALTH_URL


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class HealthProbeResult:
    status: str
    requests: int
    probed_at: datetime
    stale: bool = False


class LoopbackHealthClient:
    """Real HTTP health probe against the separate local demo target."""

    def __init__(self, url: str = LOCAL_DEMO_TARGET_HEALTH_URL) -> None:
        self.url = url

    async def probe_fresh(self) -> HealthProbeResult:
        probed_at = utcnow()
        try:
            async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
                response = await client.get(self.url)
                response.raise_for_status()
                data = response.json()
                if data.get("status") != "healthy" or data.get("mutations_received") != 0:
                    raise ValueError("Target health mismatch")
                return HealthProbeResult(
                    status="healthy",
                    requests=int(data["requests"]),
                    probed_at=probed_at,
                )
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return HealthProbeResult(status="unavailable", requests=0, probed_at=probed_at)


async def verify_health_for_reset(client: LoopbackHealthClient) -> HealthProbeResult:
    """Fail closed unless a fresh successful probe is available."""
    result = await client.probe_fresh()
    if result.status != "healthy" or result.stale or is_probe_stale(result):
        raise PortError("CONFLICT")
    return result


def is_probe_stale(result: HealthProbeResult, *, now: datetime | None = None) -> bool:
    now = now or utcnow()
    return (now - result.probed_at) > timedelta(seconds=HEALTH_FRESH_SECONDS)
