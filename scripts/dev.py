#!/usr/bin/env python3
"""Run ChaosInject locally with one command.

  python scripts/dev.py setup            # venv + Python deps + dashboard build (once)
  python scripts/dev.py up [--execute] [--llm MODEL [--llm-base-url URL]]
                                         # hotel sandbox + observability + API/dashboard
  python scripts/dev.py status
  python scripts/dev.py down [--force]   # stops everything; data volumes are kept

Stdlib only, so `setup` works before the venv exists. Every port stays on 127.0.0.1.

--llm MODEL turns on language-model explanations (docs/llm.md). The endpoint is any OpenAI-compatible API and
has no default: set LLM_BASE_URL (Groq: https://api.groq.com/openai/v1; local Ollama:
http://127.0.0.1:11434/v1) in the shell, in a git-ignored .env, or with --llm-base-url. A hosted endpoint needs
a key: LLM_API_KEY (or GROQ_KEY) from the environment, else from the .env file. The key is never taken from an
argument and never written to disk, logs or output; it goes only into the API process's environment.
"""
import argparse
import json
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
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
ENV_FILE = ROOT / ".env"
HOTELS_URL = "http://127.0.0.1:5000/hotels?inDate=2015-04-09&outDate=2015-04-10&lat=37.7749&lon=-122.4194"


def run(*args, cwd=ROOT):
    print("+", " ".join(str(a) for a in args), flush=True)
    subprocess.run([str(a) for a in args], cwd=cwd, check=True)


DOCKER_INSTALL = {
    "Windows": "Install Docker Desktop (https://www.docker.com/products/docker-desktop/) with the WSL 2 backend "
               "(required on Windows Home), start it, then run this again.",
    "Darwin": "Install Docker Desktop (https://www.docker.com/products/docker-desktop/), start it, then run this again. "
              "On Apple Silicon turn on amd64 emulation.",
    "Linux": "Install Docker Engine and the Compose plugin (https://docs.docker.com/engine/install/), then run this again.",
}
DOCKER_START = {
    "Windows": "Start Docker Desktop and wait until it says the engine is running, then run this again. Docker Desktop "
               "needs the WSL 2 backend (required on Windows Home): turn it on in Docker Desktop's settings, and if WSL "
               "is missing run `wsl --install` in an administrator PowerShell and restart.",
    "Darwin": "Start Docker Desktop and wait until it says the engine is running, then run this again.",
    "Linux": "Start the daemon (`sudo systemctl start docker`) and make sure your user can reach it "
             "(`docker info` must work), then run this again.",
}


def docker_problem(run=subprocess.run, system=None):
    """Why Docker cannot be used, in words that say what to do. None when the engine answers."""
    system = system if system in DOCKER_INSTALL else (platform.system() if platform.system() in DOCKER_INSTALL else "Linux")
    try:
        result = run(["docker", "info"], capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        return "Docker is not installed (there is no `docker` command). " + DOCKER_INSTALL[system]
    except subprocess.TimeoutExpired:
        return "Docker did not answer within 20 seconds; its engine may still be starting. Wait a minute, then run this again."
    if result.returncode == 0:
        return None
    said = next((line.strip() for line in (result.stderr or "").splitlines() if line.strip()), "")
    return ("Docker is installed but its engine is not running, or not reachable. " + DOCKER_START[system]
            + (f" Docker said: {said[:300]}" if said else ""))


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


def parse_env_file(text):
    """KEY=value lines: spaces around '=', optional `export`, quotes, blank lines and # comments are tolerated."""
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if name:
            values[name] = value
    return values


def env_file_is_ignored(path=ENV_FILE):
    """True only if git says the file is ignored (so a key in it can never be committed)."""
    return subprocess.run(["git", "check-ignore", "-q", str(path)], cwd=ROOT, capture_output=True).returncode == 0


def llm_settings(environ, base_url_flag=None, env_path=None, ignored=None):
    """Base URL and key: environment first, then a git-ignored .env. Returns the key only in memory."""
    env_path, ignored = env_path or ENV_FILE, ignored or env_file_is_ignored
    file_values = {}
    if env_path.exists():
        wanted = ("LLM_API_KEY", "GROQ_KEY", "LLM_BASE_URL")
        if any(not environ.get(name) for name in wanted):
            if not ignored(env_path):
                raise SystemExit("Refusing to read .env: git does not ignore it, so a key in it could be committed. "
                                 "Add `.env` to .gitignore first.")
            file_values = parse_env_file(env_path.read_text(encoding="utf-8"))

    def pick(*names):
        for name in names:
            if environ.get(name):
                return environ[name], "environment"
        for name in names:
            if file_values.get(name):
                return file_values[name], ".env"
        return "", None

    key, key_source = pick("LLM_API_KEY", "GROQ_KEY")
    base_url = base_url_flag or pick("LLM_BASE_URL")[0]
    return {"base_url": base_url, "key": key, "key_source": key_source}


def _llm_location(base_url):
    return "local" if (urlparse(base_url).hostname or "") in ("127.0.0.1", "localhost", "::1") else "hosted"


def check_llm(model, base_url, api_key="", opener=urllib.request.urlopen):
    """Fail early, with a clear message, if the model endpoint cannot be used. Returns its location."""
    location = _llm_location(base_url)

    def get(url, headers=None):
        # Cloudflare in front of hosted APIs (Groq) rejects urllib's default user-agent with HTTP 403 (error 1010)
        request = urllib.request.Request(url, headers={"User-Agent": "chaosinject-dev/0.1", **(headers or {})})
        with opener(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))

    if location == "local":
        root = base_url.rstrip("/")
        root = root[: -len("/v1")] if root.endswith("/v1") else root
        try:
            names = [m.get("name", "") for m in get(root + "/api/tags").get("models", [])]
        except (OSError, ValueError):
            raise SystemExit(f"No language model server answers at {base_url}. Install Ollama, start it "
                             f"(`ollama serve`), then `ollama pull {model}`. See docs/llm.md.") from None
        if not any(n == model or n == f"{model}:latest" or n.split(":")[0] == model for n in names):
            raise SystemExit(f"Ollama is running but the model {model!r} is not pulled. Run: ollama pull {model}")
        return location
    if urlparse(base_url).scheme != "https":
        raise SystemExit(f"A hosted LLM endpoint must use https, got {base_url}")
    if not api_key:
        raise SystemExit(f"LLM_API_KEY is not set. Export it in this shell first (never pass it as an argument), "
                         f"then run the command again. Host: {urlparse(base_url).netloc}")
    try:
        data = get(base_url.rstrip("/") + "/models", {"Authorization": "Bearer " + api_key})
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SystemExit(f"{urlparse(base_url).netloc} rejected the API key (HTTP {e.code}). Check LLM_API_KEY.") from None
        raise SystemExit(f"{urlparse(base_url).netloc} answered HTTP {e.code} when listing models.") from None
    except (OSError, ValueError):
        raise SystemExit(f"Could not reach {urlparse(base_url).netloc} to check the model. Check the network and LLM_BASE_URL.") from None
    ids = [m.get("id", "") for m in data.get("data", [])]
    if ids and model not in ids:
        raise SystemExit(f"{urlparse(base_url).netloc} does not offer the model {model!r}. Some it offers: {', '.join(ids[:6])}")
    return location


def python_problem(version=sys.version_info):
    """Why this Python cannot install requirements.lock, or None. pydantic-core is pinned and has no 3.14 build yet."""
    if (3, 11) <= tuple(version[:2]) < (3, 14):
        return None
    return (f"Python {version[0]}.{version[1]} is not supported: use 3.11 to 3.13 (CI runs 3.12). If you have several, "
            "run this with one of them, for example `py -3.12 scripts/dev.py setup` on Windows or `python3.12 scripts/dev.py setup`.")


def setup(_):
    problem = python_problem()
    if problem and not VENV_PY.exists():  # an existing venv was built by whatever Python made it
        raise SystemExit(problem)
    made = not VENV_PY.exists()
    try:
        if made:
            run(sys.executable, "-m", "venv", ROOT / ".venv")
        run(VENV_PY, "-m", "pip", "install", "-r", ROOT / "requirements.lock")
    except subprocess.CalledProcessError as error:
        if made:  # a half-made venv would be reused by the next try, whichever Python that uses
            shutil.rmtree(ROOT / ".venv", ignore_errors=True)
        raise SystemExit(f"Could not set up the Python environment ({error.cmd[1:4]} exited {error.returncode}); see the output above. "
                         "On Debian/Ubuntu `python3 -m venv` needs `sudo apt install python3-venv`. "
                         + ("The half-made .venv was removed, so the next try starts clean." if made else ""))
    run(VENV_PY, "-m", "pip", "install", "--no-deps", "-e", ROOT)
    run(NPM, "ci", cwd=ROOT / "frontend")
    run(NPM, "run", "build", cwd=ROOT / "frontend")
    print("\nSetup done. Next: python scripts/dev.py up --execute")


def start_api(execute, llm=None):  # llm may carry the key; it goes only into the API process environment
    if port_in_use(API_PORT):
        raise SystemExit(f"Port {API_PORT} is already in use by another process; stop it first")
    token = secrets.token_urlsafe(32)
    env = {**os.environ, "OPERATOR_TOKEN": token, "CHAOS_EXECUTION_ENABLED": "true" if execute else "false"}
    if llm:  # LLM_API_KEY, if any, is inherited from this process's environment and goes nowhere else
        env.update(INTELLIGENCE_REASONING_MODE="live", INTELLIGENCE_LIVE_MODEL=llm["model"], LLM_BASE_URL=llm["base_url"])
        if llm.get("key"):
            env["LLM_API_KEY"] = llm["key"]
        if "gpt-oss" in llm["model"]:  # reasoning models return no text if they spend the output cap thinking
            env.setdefault("LLM_REASONING_EFFORT", "low")
    LOG.parent.mkdir(exist_ok=True)
    detach = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
              if os.name == "nt" else {"start_new_session": True})
    with open(LOG, "ab") as log:
        process = subprocess.Popen(
            [str(VENV_PY), "-m", "uvicorn", "app.main:create_app", "--factory", "--app-dir", "backend",
             "--host", "127.0.0.1", "--port", str(API_PORT)],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **detach)
    STATE.write_text(json.dumps({"pid": process.pid, "token": token, "execute": execute,
                                 "llm": llm and {"model": llm["model"], "base_url": llm["base_url"],
                                                 "key_set": bool(llm.get("key")), "key_source": llm.get("key_source")}}))
    wait_for("API + dashboard", f"http://127.0.0.1:{API_PORT}/", 60)  # /api/* needs the token


def up(args):
    llm = None
    if args.llm:
        cfg = llm_settings(os.environ, args.llm_base_url)
        base_url = cfg["base_url"]
        if not base_url:
            raise SystemExit("--llm needs LLM_BASE_URL: Groq https://api.groq.com/openai/v1, or a local Ollama "
                             "http://127.0.0.1:11434/v1. Set it in the shell, in a git-ignored .env (see .env.example), "
                             "or pass --llm-base-url.")
        location = check_llm(args.llm, base_url, cfg["key"])
        llm = {"model": args.llm, "base_url": base_url, "location": location, "key": cfg["key"], "key_source": cfg["key_source"]}
        where = "on this machine" if location == "local" else f"at {urlparse(base_url).netloc}"
        print(f"LLM explanations: {args.llm} {where}."
              + ("" if location == "local" else " Run summaries (ids, targets, numbers) are sent there when you open Findings or Suggested experiments."))
    if not VENV_PY.exists() or not (ROOT / "frontend/dist/index.html").exists():
        raise SystemExit("Run `python scripts/dev.py setup` first")
    problem = docker_problem()
    if problem:
        raise SystemExit(problem)
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
    if state and (state.get("llm") or {}).get("model") != (llm or {}).get("model"):
        raise SystemExit("The API is already running with a different --llm setting; run `down` first")
    if not state:
        start_api(args.execute, llm)
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
    problem = docker_problem()
    if problem:  # nothing to stop in Docker if there is no engine; say so instead of a traceback
        print("Dashboard stopped. The sandbox containers were not touched because Docker is not available.\n" + problem)
        return
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


def llm_label(base_url):
    host = urlparse(base_url).hostname or "endpoint"
    return "groq" if host.endswith("groq.com") else host


def status(_):
    problem = docker_problem()
    if problem:  # without an engine the container counts would be misleading ("0/0 running")
        print("docker: not available. " + problem.split(". ")[0].rstrip(".") + ". Run `python scripts/dev.py up` for what to do.")
        print("sandbox containers: unknown")
    else:
        print(stack("hotel sandbox", HOTEL))
        print(stack("observability", OBS))
    print("Prometheus:", "ready  http://127.0.0.1:9090" if http_ok("http://127.0.0.1:9090/-/ready") else "down")
    state = api_state()
    if state:
        print(f"dashboard: http://127.0.0.1:{API_PORT}")
        print("operator token:", state["token"])
        print("execution:", "ENABLED (real faults on the sandbox)" if state["execute"]
              else "disabled (start with `up --execute` to run experiments)")
        llm = state.get("llm")
        print("explanations:", (f"LLM {llm['model']} via {llm_label(llm['base_url'])}"
                                + (f", key set (from {llm['key_source']})" if llm.get("key_set") else ""))
              if llm else "rule-based (start with `up --llm MODEL` for model-written text)")
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
    p_up.add_argument("--llm", metavar="MODEL", help="model-written explanations via an OpenAI-compatible API (docs/llm.md)")
    p_up.add_argument("--llm-base-url", metavar="URL", help="default: LLM_BASE_URL from the environment or .env; Groq: https://api.groq.com/openai/v1")
    p_up.set_defaults(func=up)
    p_down = commands.add_parser("down")
    p_down.add_argument("--force", action="store_true", help="stop even if a chaos run is in progress")
    p_down.set_defaults(func=down)
    commands.add_parser("status").set_defaults(func=status)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
