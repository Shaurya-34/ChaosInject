#!/usr/bin/env python3
"""Derive a locked local sandbox from the unmodified upstream Compose file."""
import json
import os
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
APP_SERVICES = {
    "frontend", "search", "geo", "rate", "profile", "recommendation",
    "reservation", "user", "review", "attractions",
}
SOURCE_SERVICES = {"review", "attractions"}


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs)


def configure(model, lock, source):
    """Keep upstream services/volumes; change only sandbox deployment settings."""
    model = json.loads(json.dumps(model))
    services = model["services"]
    expected = APP_SERVICES | {"consul", "jaeger"} | {
        "mongodb-" + name for name in
        ("geo", "profile", "rate", "recommendation", "reservation", "user", "review", "attractions")
    } | {"memcached-" + name for name in ("rate", "profile", "reserve", "review")}
    if set(services) != expected:
        raise ValueError("Upstream service inventory changed; review before generating")
    images = lock["images"]
    model["name"] = "hotel-sandbox"
    for name, network in model.get("networks", {}).items():
        if network.get("external"):
            raise ValueError("External networks are not allowed in this local sandbox")
        network["name"] = "hotel-sandbox_" + name
    for name, volume in model.get("volumes", {}).items():
        if volume.get("external"):
            raise ValueError("External volumes are not allowed in this local sandbox")
        volume["name"] = "hotel-sandbox_" + name
    model.pop("version", None)
    model.pop("configs", None)
    for name, service in services.items():
        service["restart"] = "no"
        service.pop("deploy", None)
        service.pop("container_name", None)
        service.pop("hostname", None)
        service.pop("ports", None)
        service.pop("configs", None)
        service.pop("build", None)
        service["pull_policy"] = "missing"
        if name in APP_SERVICES:
            service["working_dir"] = "/workspace"
            service["environment"] = {
                "TLS": "false", "GC": "100", "LOG_LEVEL": "info",
                "JAEGER_SAMPLE_RATIO": "1", "MEMC_TIMEOUT": "2",
            }
            service["volumes"] = [{
                "type": "bind", "source": str(source / "config.json"),
                "target": "/workspace/config.json", "read_only": True,
            }]
            if name in SOURCE_SERVICES:
                service["image"] = "hotel-sandbox/" + name + ":" + lock["upstream_commit"][:12]
                service["build"] = {
                    "context": str(source), "dockerfile": "Dockerfile.sandbox",
                }
                service["pull_policy"] = "build"
            else:
                service["image"] = images["hotel"]["image"]
                service["platform"] = "linux/amd64"
        else:
            kind = "mongo" if name.startswith("mongodb-") else "memcached" if name.startswith("memcached-") else name
            service["image"] = images[kind]["image"]
        if name.startswith("mongodb-"):
            service["healthcheck"] = {
                "test": ["CMD", "mongo", "--quiet", "--eval", "quit(db.adminCommand('ping').ok ? 0 : 1)"],
                "interval": "5s", "timeout": "5s", "retries": 30, "start_period": "10s",
            }
        if name.startswith("memcached-"):
            service.pop("environment", None)
            service["command"] = ["memcached", "-m", "128", "-t", "2"]
        if "depends_on" in service:
            deps = service["depends_on"]
            service["depends_on"] = {
                dep: {"condition": "service_healthy" if dep == "consul" or dep.startswith("mongodb-") else "service_started"}
                for dep in deps
            }
    services["consul"]["command"] = ["agent", "-dev", "-client=0.0.0.0"]
    services["consul"]["healthcheck"] = {
        "test": ["CMD-SHELL", "wget -qO- http://127.0.0.1:8500/v1/status/leader | grep -q ':8300'"],
        "interval": "5s", "timeout": "5s", "retries": 30,
    }
    for name, port in (("frontend", 5000), ("consul", 8500), ("jaeger", 16686)):
        services[name]["ports"] = [{"target": port, "published": str(port), "host_ip": "127.0.0.1", "protocol": "tcp"}]
    services["frontend"].setdefault("depends_on", {}).update({
        name: {"condition": "service_started"} for name in APP_SERVICES - {"frontend"}
    })
    services["search"].setdefault("depends_on", {}).update({
        name: {"condition": "service_started"} for name in ("geo", "rate")
    })
    return model


def main():
    lock = json.loads((HERE / "images.lock.json").read_text())
    runtime = HERE / ".runtime"
    repo = runtime / "DeathStarBench"
    runtime.mkdir(exist_ok=True)
    if not repo.exists():
        subprocess.run(["git", "clone", "--filter=blob:none", "--sparse", "--no-checkout",
                        "https://github.com/delimitrou/DeathStarBench.git", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "core.longpaths", "true"], check=True)
        subprocess.run(["git", "-C", str(repo), "sparse-checkout", "set", "--cone",
                        "--sparse-index", "hotelReservation"], check=True)
        subprocess.run(["git", "-C", str(repo), "checkout", "--detach", lock["upstream_commit"]], check=True)
    if run("git", "-C", str(repo), "rev-parse", "HEAD").strip() != lock["upstream_commit"]:
        raise SystemExit("Existing checkout differs from lock; use a fresh .runtime directory")
    if run("git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no").strip():
        raise SystemExit("Upstream tracked files were modified; refusing to overwrite local work")
    source = repo / "hotelReservation"
    dockerfile = (source / "Dockerfile").read_text()
    original = "FROM golang:1.21 as builder"
    if dockerfile.count(original) != 1:
        raise SystemExit("Unexpected upstream Dockerfile; review required")
    (source / "Dockerfile.sandbox").write_text(dockerfile.replace(
        original, "FROM " + lock["images"]["golang"]["image"] + " as builder"))
    # Do not let host environment values change this benchmark's configuration.
    env = os.environ.copy()
    for key in ("TLS", "GC", "LOG_LEVEL", "JAEGER_SAMPLE_RATIO", "MEMC_TIMEOUT"):
        env.pop(key, None)
    model = json.loads(run("docker", "compose", "-f", str(source / "docker-compose.yml"),
                           "config", "--format", "json", env=env))
    model = configure(model, lock, source)
    output = runtime / "compose.yaml"
    output.write_text(json.dumps(model, indent=2) + "\n")
    subprocess.run(["docker", "compose", "-f", str(output), "config", "--quiet"], check=True)
    print("Prepared:", output)
    print("Next: cd", runtime)
    print("docker compose pull --ignore-buildable")
    print("docker compose up -d --build")
    print(("python" if os.name == "nt" else "python3") + " ../smoke.py")


if __name__ == "__main__":
    main()
