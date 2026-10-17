"""Workload readiness from Kubernetes, and honest handling of absent metrics.

Readiness is read through ``kubectl`` rather than the Python client, for two
reasons: no extra dependency, and the ``--context`` flag makes the sandbox
target explicit on every single call. The context is validated against the
allowlist before the process is spawned, so a stray current-context cannot
redirect a read at someone else's cluster.

On resource metrics: a default ``kind`` cluster has no metrics-server, so
``kubectl top`` fails. That failure is reported as ``missing_features``, not as
zero utilisation. We do not install a monitoring stack to fill a chart, and a
missing metric must never read as a healthy one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Protocol

from ._contracts import ReadyState
from .redaction import redact_exception

logger = logging.getLogger(__name__)


class SandboxContextError(RuntimeError):
    """The requested kube context is not in the allowlist."""


@dataclass(frozen=True)
class ClusterReadout:
    """One read of workload readiness plus whatever resource data existed."""

    ready_by_workload: dict[str, ReadyState]
    cpu_utilization: float | None
    memory_utilization: float | None
    missing_features: list[str]


class ClusterAdapter(Protocol):
    async def read(self) -> ClusterReadout: ...


class KubectlClusterAdapter:
    """Live adapter. Read-only: ``get`` and ``top`` only, never a mutation.

    This module is incapable of changing cluster state by construction --
    mutations belong to Tooshita's runtime, and keeping that boundary sharp is
    part of why telemetry can hold a narrower RBAC role.
    """

    def __init__(
        self,
        context: str,
        namespace: str,
        allowed_contexts: frozenset[str],
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        if context not in allowed_contexts:
            raise SandboxContextError(
                f"context {context!r} is not in the sandbox allowlist; refusing to read"
            )
        self._context = context
        self._namespace = namespace
        self._timeout = timeout_seconds

    async def _kubectl(self, *args: str) -> tuple[int, bytes]:
        cmd = (
            "kubectl",
            "--context",
            self._context,
            "--namespace",
            self._namespace,
            *args,
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, b""
        return proc.returncode or 0, stdout

    async def read(self) -> ClusterReadout:
        missing: list[str] = []
        ready: dict[str, ReadyState] = {}

        code, stdout = await self._kubectl("get", "deployments", "-o", "json")
        if code != 0 or not stdout:
            missing.append("ready_by_workload")
        else:
            try:
                payload = json.loads(stdout)
                for item in payload.get("items", []):
                    name = item.get("metadata", {}).get("name")
                    if not name:
                        continue
                    status = item.get("status", {})
                    spec = item.get("spec", {})
                    ready[name] = ReadyState(
                        ready=int(status.get("readyReplicas", 0) or 0),
                        desired=int(spec.get("replicas", 0) or 0),
                    )
            except (ValueError, TypeError) as exc:
                logger.warning("readiness parse failed: %s", redact_exception(exc))
                missing.append("ready_by_workload")
                ready = {}

        cpu, mem = await self._read_resource_utilisation(missing)
        return ClusterReadout(
            ready_by_workload=ready,
            cpu_utilization=cpu,
            memory_utilization=mem,
            missing_features=missing,
        )

    async def _read_resource_utilisation(
        self, missing: list[str]
    ) -> tuple[float | None, float | None]:
        """Best-effort ``kubectl top``.

        Returns ``(None, None)`` and records missing features whenever
        metrics-server is absent, which is the default on kind. Absent is
        absent -- it is never reported as 0% utilisation.
        """
        code, _ = await self._kubectl("top", "pods", "--no-headers")
        if code != 0:
            missing.extend(["cpu_utilization", "memory_utilization"])
            return None, None
        # metrics-server reports absolute usage, not a utilisation fraction.
        # Turning that into a percentage needs per-pod requests/limits, which we
        # have not implemented, so the honest answer is still "unknown".
        missing.extend(
            [
                "cpu_utilization (metrics-server present; "
                "utilisation fraction not implemented)",
                "memory_utilization (metrics-server present; "
                "utilisation fraction not implemented)",
            ]
        )
        return None, None


class StaticClusterAdapter:
    """Fake adapter for tests, fixtures and the stub-server slice."""

    def __init__(self, readout: ClusterReadout) -> None:
        self._readout = readout

    def set(self, readout: ClusterReadout) -> None:
        self._readout = readout

    async def read(self) -> ClusterReadout:
        return self._readout


def all_workloads_ready(ready: dict[str, ReadyState]) -> bool:
    """True only when every workload has ready == desired and desired > 0.

    An empty map is not readiness -- it means we could not see the cluster.
    """
    if not ready:
        return False
    return all(r.desired > 0 and r.ready >= r.desired for r in ready.values())
