#!/usr/bin/env python3
"""Verify the local Hotel sandbox; reserve one room and save actual evidence."""
import datetime
import json
from pathlib import Path
import subprocess
import time
import urllib.parse
import urllib.request
import uuid

HERE = Path(__file__).resolve().parent


def request(port, path, params=None):
    url = "http://127.0.0.1:" + str(port) + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=10) as response:
        body = response.read().decode()
        return response.status, body


def retry(label, check, evidence, seconds=180):
    deadline = time.monotonic() + seconds
    while True:
        try:
            value = check()
            evidence[label] = {"passed": True, "result": value}
            print("PASS", label)
            return value
        except Exception as error:
            if time.monotonic() >= deadline:
                evidence[label] = {"passed": False, "error": str(error)}
                raise RuntimeError(label + ": " + str(error)) from error
            time.sleep(2)


def features(path, params):
    status, body = request(5000, path, params)
    data = json.loads(body)
    if status != 200 or data.get("type") != "FeatureCollection" or not data.get("features"):
        raise ValueError("Expected a non-empty GeoJSON FeatureCollection: " + body[:500])
    return [str(item["id"]) for item in data["features"]]


def login():
    # The pinned source formats the suffix in hex; older prebuilt images may
    # use its decimal form. Both variants use ten zeroes for test user zero.
    for username in ("Cornell_0", "Cornell_30"):
        _, body = request(5000, "/user", {"username": username, "password": "0" * 10})
        message = json.loads(body).get("message", "")
        if "success" in message.lower() and "fail" not in message.lower():
            _, bad = request(5000, "/user", {"username": username, "password": "wrong-password"})
            if "fail" not in json.loads(bad).get("message", "").lower():
                raise ValueError("Invalid login was not rejected")
            return username
    raise ValueError("Neither known seeded login succeeded")


def compose(*args):
    return subprocess.check_output(["docker", "compose", "-f", str(HERE / ".runtime/compose.yaml"),
                                    *args], text=True, timeout=30)


def containers():
    model = json.loads(compose("config", "--format", "json"))
    output = compose("ps", "--all", "--format", "json").strip()
    rows = json.loads(output) if output.startswith("[") else [json.loads(line) for line in output.splitlines()]
    running = {row["Service"] for row in rows if row["State"] == "running"}
    missing = set(model["services"]) - running
    if missing:
        raise ValueError("Services not running: " + ", ".join(sorted(missing)))
    ids = [row["ID"] for row in rows]
    policies = json.loads(subprocess.check_output(["docker", "inspect", *ids], text=True, timeout=30))
    if any(row["HostConfig"]["RestartPolicy"]["Name"] not in ("no", "") for row in policies):
        raise ValueError("Automatic restart is still enabled on a sandbox container")
    return {"running": sorted(running), "automatic_restart": False}


def main():
    evidence = {"time_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "image_lock": json.loads((HERE / "images.lock.json").read_text()),
                "scope": "Local HTTP flows, MongoDB reservation persistence, service state, Jaeger"}
    identifier = uuid.uuid4().hex
    destination = HERE.parents[1] / "results" / ("hotel-smoke-" + identifier + ".json")
    try:
        retry("containers", containers, evidence)
        def ui():
            status, body = request(16686, "/")
            if status != 200 or "<html" not in body.lower():
                raise ValueError("Jaeger UI did not return HTML")
            return {"status": status, "url": "http://localhost:16686"}
        retry("jaeger_ui", ui, evidence)
        username = retry("login", login, evidence)
        dates = {"inDate": "2015-04-09", "outDate": "2015-04-10"}
        location = {"lat": "37.7749", "lon": "-122.4194"}
        hotels = retry("search", lambda: features("/hotels", {**dates, **location}), evidence)
        retry("recommendation", lambda: features("/recommendations", {**location, "require": "dis"}), evidence)
        customer = "smoke-" + identifier
        # A reservation is a write: never retry the HTTP call automatically.
        _, body = request(5000, "/reservation", {
            **dates, "hotelId": hotels[0], "customerName": customer,
            "username": username, "password": "0" * 10, "number": "1",
        })
        message = json.loads(body).get("message", "")
        if "success" not in message.lower() or "fail" in message.lower():
            raise ValueError("Reservation failed: " + body)
        query = "db.getSiblingDB('reservation-db').reservation.countDocuments(" + json.dumps({"customerName": customer}) + ")"
        count = int(compose("exec", "-T", "mongodb-reservation", "mongo", "--quiet", "--eval", query).strip())
        if count < 1:
            raise ValueError("Reservation response was successful but no MongoDB write was found")
        evidence["reservation"] = {"passed": True, "customer": customer, "hotel": hotels[0], "documents": count}
        print("PASS reservation (verified MongoDB write)")
        def traces():
            _, body = request(16686, "/api/services")
            names = json.loads(body).get("data", [])
            if "frontend" not in names:
                raise ValueError("No frontend service visible in Jaeger yet")
            return names
        retry("jaeger_services", traces, evidence, seconds=30)
        evidence["passed"] = True
    except Exception as error:
        evidence["passed"] = False
        evidence["error"] = str(error)
        raise
    finally:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(evidence, indent=2) + "\n")
        print("Evidence:", destination)


if __name__ == "__main__":
    main()
