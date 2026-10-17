"""HTTP transport behind a narrow protocol.

The probe never imports ``httpx`` directly. It talks to ``HttpTransport``, which
has exactly one real implementation and one fake. That buys three things:

* the whole probe pipeline is testable with no network and no cluster
* ``httpx`` stays a lazy import, so the telemetry package can be imported in an
  environment that does not have it installed
* the response object exposed to the probe is deliberately *not* an ``httpx``
  response -- it carries a status, a bounded body slice for marker matching, and
  nothing else. Cookies, headers and full bodies never leave this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# How much of a response body is read for semantic marker matching. Bounded so a
# large page cannot blow up memory, and so nothing resembling a full page body is
# ever held beyond the matching call.
MARKER_SCAN_BYTES = 8192


@dataclass(frozen=True, slots=True)
class ProbeResponse:
    """Minimal, already-bounded view of an HTTP response.

    ``body_head`` exists only to answer "did the expected marker render?". It is
    never logged, never exported, and never retained past marker evaluation.
    """

    status_code: int
    body_head: str
    elapsed_ms: float


class TransportTimeout(Exception):
    """The request did not complete within the configured timeout."""


class TransportConnectionError(Exception):
    """The endpoint could not be reached."""


class HttpTransport(Protocol):
    """One synthetic session's worth of HTTP capability."""

    async def get(self, path: str) -> ProbeResponse: ...

    async def post(self, path: str, form: dict[str, str]) -> ProbeResponse: ...

    async def aclose(self) -> None: ...


class HttpxTransport:
    """Real transport. One instance per synthetic session.

    Each instance owns its own cookie jar, which is how sessions stay isolated:
    one synthetic shopper's cart never leaks into another's. The jar is destroyed
    with the instance and is never read, logged or exported.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float,
        user_agent: str = "bosch-resilience-ai-probe/1.0",
    ) -> None:
        try:
            import httpx  # noqa: PLC0415 - deliberately lazy
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "httpx is required for the live probe transport; "
                "add it to the root dependency lock (Sachin owns the lock)"
            ) from exc

        self._httpx = httpx
        self._timeout_seconds = timeout_seconds
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        )

    async def _request(self, method: str, path: str, **kwargs) -> ProbeResponse:
        import time

        started = time.monotonic()
        try:
            response = await self._client.request(method, path, **kwargs)
        except self._httpx.TimeoutException as exc:
            raise TransportTimeout() from exc
        except self._httpx.TransportError as exc:
            raise TransportConnectionError() from exc
        elapsed_ms = (time.monotonic() - started) * 1000.0
        body_head = response.text[:MARKER_SCAN_BYTES]
        return ProbeResponse(
            status_code=response.status_code,
            body_head=body_head,
            elapsed_ms=elapsed_ms,
        )

    async def get(self, path: str) -> ProbeResponse:
        return await self._request("GET", path)

    async def post(self, path: str, form: dict[str, str]) -> ProbeResponse:
        return await self._request("POST", path, data=form)

    async def aclose(self) -> None:
        await self._client.aclose()
