"""Read-only Kubernetes inventory adapter and fixture implementation.

Live access is deliberately explicit: callers must provide an enrolled identity,
the exact context, and the namespace UID preimage. This adapter never mutates.
"""

from __future__ import annotations

import json
import base64
import hashlib
import re
import subprocess
from pathlib import Path
from typing import Protocol

from .models import (
    SandboxIdentity,
    SandboxMismatch,
    TopologyEdge,
    TopologyNode,
    TopologySnapshot,
    VerifiedTarget,
    utc_now,
)


class PlatformPort(Protocol):
    def get_topology(self, sandbox_id: str) -> TopologySnapshot: ...
    def get_target(self, target_id: str) -> VerifiedTarget: ...


class FixturePlatform:
    def __init__(self, snapshot: TopologySnapshot, targets: dict[str, VerifiedTarget]):
        self.snapshot = snapshot
        self.targets = targets

    def get_topology(self, sandbox_id: str) -> TopologySnapshot:
        if sandbox_id != self.snapshot.sandbox_id:
            raise SandboxMismatch("sandbox_id does not match fixture")
        return self.snapshot

    def get_target(self, target_id: str) -> VerifiedTarget:
        try:
            return self.targets[target_id]
        except KeyError as exc:
            raise KeyError("unknown verified target") from exc


def load_fixture(path: str | Path) -> FixturePlatform:
    """Load a topology fixture while preserving its explicit source label."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != "1.0" or data.get("source") != "fixture":
        raise ValueError("platform fixture must declare schema_version 1.0 and source fixture")
    nodes = tuple(TopologyNode(
        resource_id=n["resource_id"], kind=n["kind"], namespace=n["namespace"], name=n["name"],
        uid=n.get("uid"), selector=n.get("selector", {}), ports=tuple(n.get("ports", [])),
        desired_replicas=n.get("desired_replicas"), ready_replicas=n.get("ready_replicas"),
        stateful=n.get("stateful"), confidence=n.get("confidence", 1.0),
    ) for n in data.get("nodes", []))
    edges = tuple(TopologyEdge(**e) for e in data.get("edges", []))
    snapshot = TopologySnapshot(
        graph_id=data["graph_id"], upstream_sha=data.get("upstream_sha"),
        generated_at=data["generated_at"], sandbox_id=data["sandbox_id"],
        namespace=data["namespace"], nodes=nodes, edges=edges,
        manual_corrections=tuple(data.get("manual_corrections", [])), source="fixture",
    )
    # Fixtures are for discovery/UI contracts only and deliberately yield no
    # mutation-eligible targets because they lack live UIDs and reviewed policy.
    return FixturePlatform(snapshot, {})


class KubectlPlatform:
    """Read namespaced workloads and services using an explicit kubectl context."""

    def __init__(self, identity: SandboxIdentity, context: str, namespace: str):
        if context != identity.context:
            raise SandboxMismatch("requested context differs from enrolled context")
        if namespace not in identity.namespace_uids:
            raise SandboxMismatch("namespace is not enrolled")
        if not identity.api_endpoint.startswith("https://") or not identity.ca_fingerprint:
            raise SandboxMismatch("sandbox identity lacks API endpoint or CA fingerprint")
        self.identity, self.context, self.namespace = identity, context, namespace
        self._verify_cluster_identity()
        self._verify_namespace()

    def _kubectl(self, *args: str) -> dict:
        result = subprocess.run(
            ["kubectl", "--context", self.context, "-n", self.namespace, *args, "-o", "json"],
            check=True, capture_output=True, text=True, timeout=20,
        )
        return json.loads(result.stdout)

    def _verify_namespace(self) -> None:
        result = subprocess.run(
            ["kubectl", "--context", self.context, "get", "namespace", self.namespace, "-o", "json"],
            check=True, capture_output=True, text=True, timeout=20,
        )
        observed = json.loads(result.stdout).get("metadata", {}).get("uid")
        if observed != self.identity.namespace_uids[self.namespace]:
            raise SandboxMismatch("namespace UID differs from enrolled identity")

    def _verify_cluster_identity(self) -> None:
        result = subprocess.run(
            ["kubectl", "--context", self.context, "config", "view", "--minify", "-o", "json"],
            check=True, capture_output=True, text=True, timeout=20,
        )
        config = json.loads(result.stdout)
        clusters = config.get("clusters", [])
        if len(clusters) != 1:
            raise SandboxMismatch("explicit context did not resolve to exactly one cluster")
        cluster = clusters[0].get("cluster", {})
        if cluster.get("server") != self.identity.api_endpoint:
            raise SandboxMismatch("Kubernetes API endpoint differs from enrolled identity")
        encoded_ca = cluster.get("certificate-authority-data")
        if not encoded_ca:
            raise SandboxMismatch("Kubeconfig does not expose CA data for identity verification")
        ca_bytes = base64.b64decode(encoded_ca, validate=True)
        actual = "sha256:" + hashlib.sha256(ca_bytes).hexdigest()
        if actual.lower() != self.identity.ca_fingerprint.lower():
            raise SandboxMismatch("Kubernetes CA fingerprint differs from enrolled identity")

    def get_topology(self, sandbox_id: str) -> TopologySnapshot:
        if sandbox_id != self.identity.sandbox_id:
            raise SandboxMismatch("sandbox_id does not match enrolled identity")
        deployments = self._kubectl("get", "deployments,statefulsets").get("items", [])
        services = self._kubectl("get", "services").get("items", [])
        nodes: list[TopologyNode] = []
        stateful_by_selector: dict[tuple[str, str], bool] = {}
        for item in [*deployments, *services]:
            meta, spec, status = item.get("metadata", {}), item.get("spec", {}), item.get("status", {})
            kind, name = item.get("kind", "Unknown"), meta.get("name", "")
            selector_data = spec.get("selector", {}) or {}
            selector = selector_data.get("matchLabels", selector_data) or {}
            is_workload = kind in {"Deployment", "StatefulSet"}
            replicas = spec.get("replicas") if is_workload else None
            pod_spec = spec.get("template", {}).get("spec", {}) if is_workload else {}
            if is_workload:
                ports = tuple(p for container in pod_spec.get("containers", []) for p in container.get("ports", []))
                has_claim = any("persistentVolumeClaim" in v for v in pod_spec.get("volumes", []))
                image_names = [c.get("image", "").split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0].lower() for c in pod_spec.get("containers", [])]
                stateful = kind == "StatefulSet" or has_claim or any(name in {"redis", "postgres", "postgresql", "mysql", "mariadb"} for name in image_names)
                labels = (item.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {}) or {})
                for label, value in labels.items():
                    stateful_by_selector[(label, value)] = stateful_by_selector.get((label, value), False) or stateful
            else:
                ports = tuple(spec.get("ports", []) or [])
                matched = [stateful_by_selector[(k, v)] for k, v in selector.items() if (k, v) in stateful_by_selector]
                stateful = any(matched) if matched else None
            nodes.append(TopologyNode(
                resource_id=f"{self.namespace}/{kind.lower()}/{name}", kind=kind,
                namespace=self.namespace, name=name, uid=meta.get("uid"),
                selector=selector, ports=ports, desired_replicas=replicas,
                ready_replicas=status.get("readyReplicas") if is_workload else None,
                stateful=stateful,
            ))
        # Only emit an edge when workload configuration contains an explicit
        # service DNS/name reference. Mere co-location in a namespace is not proof.
        service_names = {s.get("metadata", {}).get("name", "") for s in services}
        edges: dict[tuple[str, str], TopologyEdge] = {}
        for item in deployments:
            meta = item.get("metadata", {})
            caller = f"{self.namespace}/deployment/{meta.get('name', '')}"
            pod_spec = item.get("spec", {}).get("template", {}).get("spec", {})
            containers = [*pod_spec.get("containers", []), *pod_spec.get("initContainers", [])]
            for container in containers:
                env_strings = [e.get("value", "") for e in container.get("env", [])]
                env_strings += list(container.get("command", [])) + list(container.get("args", []))
                for value in env_strings:
                    for candidate in service_names:
                        if candidate and re.search(rf"(?<![a-z0-9-]){re.escape(candidate)}(?![a-z0-9-])", value, re.I):
                            key = (caller, candidate)
                            edges[key] = TopologyEdge(
                                caller=caller,
                                dependency=f"{self.namespace}/service/{candidate}",
                                protocol="configured endpoint",
                                evidence="explicit workload environment/command reference",
                                source_ref=f"deployment/{meta.get('name')}/container/{container.get('name')}",
                                confidence=0.8,
                            )
        return TopologySnapshot(
            graph_id=f"{sandbox_id}:{self.namespace}:{utc_now()}", upstream_sha=None,
            generated_at=utc_now(), sandbox_id=sandbox_id, namespace=self.namespace,
            nodes=tuple(nodes), edges=tuple(edges.values()), source="live",
        )

    def get_target(self, target_id: str) -> VerifiedTarget:
        snapshot = self.get_topology(self.identity.sandbox_id)
        for node in snapshot.nodes:
            if node.resource_id != target_id:
                continue
            if not node.uid or node.kind != "Deployment" or not node.selector or node.stateful is not False:
                raise KeyError("resource is not an eligible verified Deployment target")
            return VerifiedTarget(
                target_id=target_id, sandbox_id=self.identity.sandbox_id,
                namespace=self.namespace, kind=node.kind, name=node.name, uid=node.uid,
                selector=node.selector, allowed_fault_ids=(), dependency_refs=(),
                provenance="live Kubernetes inventory; fault eligibility not yet approved",
            )
        raise KeyError("unknown verified target")
