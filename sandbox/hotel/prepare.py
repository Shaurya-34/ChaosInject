#!/usr/bin/env python3
"""Derive a locked local sandbox from the unmodified upstream Compose file.

--fix-cache builds `rate`, `profile` and `reservation` from source with a patch: a memcached error
falls back to MongoDB instead of crashing the service (upstream calls log.Panic), and a circuit
breaker (patches/cache_breaker.go) skips the cache for a cooldown so requests don't each wait for
the cache timeout. Without it, the sandbox reproduces the upstream behaviour that ChaosInject found.
"""
import argparse
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


def cache_edits(panic_line, set_line):
    """Edits (original, replacement, expected count) to an upstream server.go.

    rate and profile: an error branch that panics is turned into a MongoDB fallback.
    """
    return [
        ("\tresMap, err := s.MemcClient.GetMulti(hotelIds)\n",
         "\t// ChaosInject patch: skip the cache while its breaker is open (see cache_breaker.go).\n"
         "\tvar resMap map[string]*memcache.Item\n"
         "\tvar err error\n"
         "\tif cacheAvailable() {\n"
         "\t\tresMap, err = s.MemcClient.GetMulti(hotelIds)\n"
         "\t}\n", 1),
        (panic_line + "\t} else {\n",
         "\t\t// ChaosInject patch: a cache outage falls back to MongoDB instead of crashing the service.\n"
         '\t\tlog.Warn().Msgf("Memcached error for hotels %v: %s; falling back to MongoDB", hotelIds, err)\n'
         "\t\tmarkCacheDown()\n"
         "\t\tresMap = nil\n"
         "\t}\n"
         "\t{\n", 1),
        (set_line, "\t\t\t\tif cacheAvailable() {\n\t" + set_line + "\t\t\t\t}\n", 1),
    ]


def wrapper_edits(**counts):
    """reservation already reads MongoDB on memcache.ErrCacheMiss; route every client call
    through the cache_breaker.go wrappers so an outage looks like a miss. counts: Get, GetMulti, Set."""
    return [(f"s.MemcClient.{call}(", f"cache{call}(s.MemcClient, ", n) for call, n in counts.items()]


CACHE_FIXES = {
    # The second edit fixes a separate upstream bug that an empty cache exposes: the capacity query
    # puts $in at the top level, which MongoDB rejects, so the nil cursor then panics (server.go:271).
    "reservation": wrapper_edits(Get=2, GetMulti=2, Set=4) + [
        ('numCollection.Find(context.TODO(), bson.D{{"$in", queryMissKeys}})',
         'numCollection.Find(context.TODO(), bson.D{{"hotelId", bson.D{{"$in", queryMissKeys}}}})', 1)],
    "rate": cache_edits(
        '\t\tlog.Panic().Msgf("Memmcached error while trying to get hotel [id: %v]= %s", hotelIds, err)\n',
        "\t\t\t\tgo s.MemcClient.Set(&memcache.Item{Key: id, Value: []byte(memcStr)})\n"),
    "profile": cache_edits(
        '\t\tlog.Panic().Msgf("Tried to get hotelIds [%v], but got memmcached error = %s", hotelIds, err)\n',
        "\t\t\t\tgo s.MemcClient.Set(&memcache.Item{Key: hotelId, Value: []byte(memcStr)})\n"),
}


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs)


def configure(model, lock, source, fix_cache=False):
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
    for name in CACHE_FIXES if fix_cache else ():
        service = services[name]
        service.pop("platform", None)
        service["image"] = "hotel-sandbox/" + name + ":" + lock["upstream_commit"][:12] + "-cache-fallback"
        service["build"] = {"context": str(source), "dockerfile": "Dockerfile.cache-fix"}
        service["pull_policy"] = "build"
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


def write_cache_fix(source, sandbox_dockerfile):
    """Patched copies of the services' sources (untracked) plus a Dockerfile that builds with them."""
    breaker = (HERE / "patches/cache_breaker.go").read_text()
    for name, edits in CACHE_FIXES.items():
        server = (source / "services" / name / "server.go").read_text()
        for original, replacement, count in edits:
            if server.count(original) != count:
                raise SystemExit(f"Unexpected upstream services/{name}/server.go; review the cache patch")
            server = server.replace(original, replacement)
        out = source / "sandbox-patches" / name
        out.mkdir(parents=True, exist_ok=True)
        (out / "server.go").write_text(server, newline="\n")
        (out / "cache_breaker.go").write_text(breaker.replace("package PACKAGE", "package " + name), newline="\n")
    copy = "COPY services/ services/\n"
    if sandbox_dockerfile.count(copy) != 1:
        raise SystemExit("Unexpected upstream Dockerfile; review the cache patch")
    (source / "Dockerfile.cache-fix").write_text(sandbox_dockerfile.replace(
        copy, copy + "".join(f"COPY sandbox-patches/{name}/ services/{name}/\n" for name in CACHE_FIXES)))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fix-cache", action="store_true",
                        help="build rate, profile and reservation with the cache-fallback patch instead of the upstream images")
    args = parser.parse_args()
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
    sandbox_dockerfile = dockerfile.replace(original, "FROM " + lock["images"]["golang"]["image"] + " as builder")
    (source / "Dockerfile.sandbox").write_text(sandbox_dockerfile)
    if args.fix_cache:
        write_cache_fix(source, sandbox_dockerfile)
    # Do not let host environment values change this benchmark's configuration.
    env = os.environ.copy()
    for key in ("TLS", "GC", "LOG_LEVEL", "JAEGER_SAMPLE_RATIO", "MEMC_TIMEOUT"):
        env.pop(key, None)
    model = json.loads(run("docker", "compose", "-f", str(source / "docker-compose.yml"),
                           "config", "--format", "json", env=env))
    model = configure(model, lock, source, fix_cache=args.fix_cache)
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
