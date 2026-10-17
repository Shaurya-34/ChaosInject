#!/usr/bin/env python3
"""Run ChaosInject locally with one command.

  python scripts/dev.py setup            # venv + Python deps + dashboard build (once)
  python scripts/dev.py up [--execute]   # hotel sandbox + observability + API/dashboard
  python scripts/dev.py status
  python scripts/dev.py down [--force]   # stops everything; data volumes are kept

Stdlib only, so `setup` works before the venv exists. Every port stays on 127.0.0.1.
"""
import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from chaos.controller import pid_alive, read_lock  # noqa: E402  (stdlib-only module)

VENV_PY = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
NPM = "npm.cmd" if os.name == "nt" else "npm"
HOTEL = ROOT / "sandbox/hotel/.runtime/compose.yaml"
OBS = ROOT / "sandbox/observability/compose.yaml"
STATE = ROOT / ".dev/api.json"
LOG = ROOT / ".dev/api.log"
API_PORT = 8000
HOTELS_URL = "http://127.0.0.1:5000/hotels?inDate=2015-04-09&outDate=2015-04-10&lat=37.7749&lon=-122.4194"


def run(*args, cwd=ROOT):
    print("+", " ".join(str(a) for a in args), flush=True)
    subprocess.run([str(a) for a in args], cwd=cwd, check=True)


def compose(file, *args, capture=False):
    cmd = ["docker", "compose", "-f", str(file), *args]
    if capture:
        return subprocess.run(cmd, text=True, capture_output=True).stdout
    run(*cmd)


def http_ok(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def wait_for(label, url, seconds):
    deadline = time.monotonic() + seconds
    while not http_ok(url):
        if time.monotonic() > deadline:
            raise SystemExit(f"{label} did not become ready at {url} within {seconds}s")
        time.sleep(2)
    print("ready:", label, flush=True)


def port_in_use(port):
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def api_state():
    """The recorded API process, or None if it is not running."""
    try:
        state = json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        return None
    return state if pid_alive(state["pid"]) else None


def setup(_):
    if not VENV_PY.exists():
        run(sys.executable, "-m", "venv", ROOT / ".venv")
    run(VENV_PY, "-m", "pip", "install", "-r", ROOT / "requirements.lock")
    run(VENV_PY, "-m", "pip", "install", "--no-deps", "-e", ROOT)
    run(NPM, "ci", cwd=ROOT / "frontend")
    run(NPM, "run", "build", cwd=ROOT / "frontend")
    print("\nSetup done. Next: python scripts/dev.py up --execute")


def start_api(execute):
    if port_in_use(API_PORT):
        raise SystemExit(f"Port {API_PORT} is already in use by another process; stop it first")
    token = secrets.token_urlsafe(32)
    env = {**os.environ, "OPERATOR_TOKEN": token, "CHAOS_EXECUTION_ENABLED": "true" if execute else "false"}
    LOG.parent.mkdir(exist_ok=True)
    detach = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
              if os.name == "nt" else {"start_new_session": True})
    with open(LOG, "ab") as log:
        process = subprocess.Popen(
            [str(VENV_PY), "-m", "uvicorn", "app.main:create_app", "--factory", "--app-dir", "backend",
             "--host", "127.0.0.1", "--port", str(API_PORT)],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **detach)
    STATE.write_text(json.dumps({"pid": process.pid, "token": token, "execute": execute}))
    wait_for("API + dashboard", f"http://127.0.0.1:{API_PORT}/", 60)  # /api/* needs the token


def up(args):
    if not VENV_PY.exists() or not (ROOT / "frontend/dist/index.html").exists():
        raise SystemExit("Run `python scripts/dev.py setup` first")
    if subprocess.run(["docker", "info"], capture_output=True).returncode:
        raise SystemExit("Docker is not running")
    if not HOTEL.exists():
        run(sys.executable, ROOT / "sandbox/hotel/prepare.py")
    compose(HOTEL, "pull", "--ignore-buildable", "--quiet")
    compose(HOTEL, "up", "-d", "--build", "--quiet-pull")
    wait_for("hotel frontend", HOTELS_URL, 300)
    compose(OBS, "up", "-d", "--quiet-pull")
    wait_for("Prometheus", "http://127.0.0.1:9090/-/ready", 60)
    state = api_state()
    if state and state["execute"] != args.execute:
        raise SystemExit("The API is already running with a different --execute setting; run `down` first")
    if not state:
        start_api(args.execute)
    status(args)


def stop_api():
    state = api_state()
    if state:
        if os.name == "nt":  # /T: the venv python.exe is a launcher with a child process
            subprocess.run(["taskkill", "/PID", str(state["pid"]), "/T", "/F"], capture_output=True)
        else:
            os.killpg(state["pid"], signal.SIGTERM)
        print("stopped: API", flush=True)
    STATE.unlink(missing_ok=True)


def down(args):
    run_lock = read_lock()
    if run_lock and not args.force:
        raise SystemExit(f"A chaos run is in progress (pid {run_lock['pid']}); stop it from the dashboard "
                         "and let it restore, or pass --force")
    stop_api()
    compose(OBS, "down")
    if HOTEL.exists():
        compose(HOTEL, "down")


def stack(label, file):
    if not file.exists():
        return f"{label}: not prepared"
    rows = [json.loads(line) for line in compose(file, "ps", "--all", "--format", "json", capture=True).splitlines()
            if line.strip()]
    running = sum(row["State"] == "running" for row in rows)
    return f"{label}: {running}/{len(rows)} containers running"


def status(_):
    print(stack("hotel sandbox", HOTEL))
    print(stack("observability", OBS))
    print("Prometheus:", "ready  http://127.0.0.1:9090" if http_ok("http://127.0.0.1:9090/-/ready") else "down")
    state = api_state()
    if state:
        print(f"dashboard: http://127.0.0.1:{API_PORT}")
        print("operator token:", state["token"])
        print("execution:", "ENABLED (real faults on the sandbox)" if state["execute"]
              else "disabled (start with `up --execute` to run experiments)")
    else:
        print("API: not running")
    run_lock = read_lock()
    print("active run:", f"pid {run_lock['pid']}" if run_lock else "none")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup").set_defaults(func=setup)
    p_up = commands.add_parser("up")
    p_up.add_argument("--execute", action="store_true", help="allow the dashboard to inject real faults")
    p_up.set_defaults(func=up)
    p_down = commands.add_parser("down")
    p_down.add_argument("--force", action="store_true", help="stop even if a chaos run is in progress")
    p_down.set_defaults(func=down)
    commands.add_parser("status").set_defaults(func=status)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
