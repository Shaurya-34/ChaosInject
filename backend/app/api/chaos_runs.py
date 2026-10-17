"""Real chaos runs: wraps chaos.controller (as a subprocess) and chaos.hooks (results store).

Reads are always allowed. Starting, stopping and recovering need CHAOS_EXECUTION_ENABLED=true.
The controller runs in its own process group, so it keeps running (and restores its fault) if
the API exits; it stops via results/.stop_request, which the controller checks every probe.
Run state comes from the controller's lock file (results/.active_run.json), not API memory, so
it survives an API restart.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from fastapi import APIRouter

from app.api.errors import PortError

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:  # chaos/ lives at the repo root, beside backend/
    sys.path.insert(0, str(ROOT))

from chaos import controller, hooks  # noqa: E402

EXPERIMENTS_DIR = ROOT / "experiments"
NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
LOG_TAIL_LINES = 40


def _log_path():
    return hooks.RESULTS_DIR / ".active_run.log"


def _spawn(experiment_path):
    """Start the controller detached from the API process; its output goes to a log file."""
    hooks.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    log = open(_log_path(), "w", encoding="utf-8")
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "CHAOS_RESULTS_DIR": str(hooks.RESULTS_DIR)}
    detach = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
              else {"start_new_session": True})
    try:
        return subprocess.Popen([sys.executable, "-m", "chaos.controller", str(experiment_path)],
                                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, **detach)
    finally:
        log.close()  # the child keeps its own handle


def _load_experiment(name):
    if not NAME.match(name):
        raise PortError("INVALID_REQUEST", 422)
    path = EXPERIMENTS_DIR / f"{name}.json"
    if not path.is_file():
        raise PortError("NOT_FOUND", 404)
    return path, json.loads(path.read_text(encoding="utf-8"))


def _policy():
    return json.loads(controller.POLICY_PATH.read_text(encoding="utf-8"))


def _log_tail():
    try:
        return _log_path().read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_TAIL_LINES:]
    except FileNotFoundError:
        return []


def _saved_run_id(lines):
    for line in reversed(lines):
        if line.startswith("saved: "):
            return Path(line[len("saved: "):].strip()).stem
    return None


def create_router(execution_enabled: bool) -> APIRouter:
    router = APIRouter(prefix="/api/v1/chaos", tags=["chaos"])
    active = {"proc": None, "experiment": None, "started_at": None}  # last run started by this API

    def running():
        # The lock covers every phase and survives API restarts; the handle covers the moment
        # between spawning and the controller taking the lock.
        spawned = active["proc"] is not None and active["proc"].poll() is None
        return spawned or controller.read_lock() is not None

    def require_execution():
        if not execution_enabled:
            raise PortError("MUTATIONS_DISABLED")

    @router.get("/experiments")
    async def experiments():
        out = []
        for path in sorted(EXPERIMENTS_DIR.glob("*.json")):
            exp = json.loads(path.read_text(encoding="utf-8"))
            out.append({"id": path.stem, "name": exp.get("name"), "target": exp.get("target"),
                        "fault": exp.get("fault"), "duration_s": exp.get("duration_s")})
        return out

    @router.post("/experiments/{name}/run", status_code=202)
    async def start_run(name: str):
        require_execution()
        path, exp = _load_experiment(name)
        try:
            controller.check_policy(exp, _policy())  # fail fast; the controller checks again
        except (controller.PolicyError, KeyError):
            raise PortError("POLICY_REFUSED", 422)
        if running() or controller.marker_path().exists():
            raise PortError("CONFLICT")
        controller.stop_path().unlink(missing_ok=True)
        active.update(proc=_spawn(path), experiment=name, started_at=time.time())
        # No pid here: on Windows a venv's python.exe is a launcher, so the controller's real pid
        # appears in GET /active once it takes the run lock.
        return {"experiment": name, "started_at": active["started_at"]}

    @router.get("/active")
    async def active_run():
        marker = controller.marker_path()
        proc = active["proc"]
        lock = controller.read_lock()
        lines = _log_tail()
        is_running = running()
        if lock:  # a run in progress, possibly started before this API process
            experiment, pid, started_at = Path(lock["experiment"]).stem, lock["pid"], lock.get("started_at")
        else:
            experiment, started_at, pid = active["experiment"], active["started_at"], None
        return {
            "running": is_running,
            "experiment": experiment,
            "pid": pid,
            "started_at": started_at,
            "exit_code": None if proc is None or is_running else proc.returncode,
            "run_id": None if is_running else _saved_run_id(lines),
            "fault_active": marker.exists(),
            "fault": json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else None,
            "stop_requested": is_running and controller.stop_path().exists(),
            "log_tail": lines,
        }

    @router.post("/active/stop", status_code=202)
    async def stop_run():
        require_execution()
        if not running():
            raise PortError("CONFLICT")
        controller.stop_path().write_text(json.dumps({"requested_at": time.time()}), encoding="utf-8")
        return {"stop_requested": True}

    @router.post("/recover")
    def recover():  # sync: restore calls docker, so FastAPI runs it in a worker thread
        """Restore a fault left behind by a controller that was killed mid-run."""
        require_execution()
        if running():
            raise PortError("CONFLICT")
        try:
            controller.acquire_lock("--recover")
        except controller.PolicyError:
            raise PortError("CONFLICT")  # a controller started in the meantime
        try:
            target = controller.recover_abandoned(controller.marker_path(), _policy(), log=lambda msg: None)
        except controller.PolicyError:
            raise PortError("POLICY_REFUSED", 422)
        finally:
            controller.release_lock()
        return {"restored": target}

    @router.get("/runs")
    async def runs():
        return list(reversed(hooks.list_results()))  # newest first

    @router.get("/runs/{run_id}")
    async def run_detail(run_id: str):
        if not RUN_ID.match(run_id) or run_id.startswith("."):
            raise PortError("INVALID_REQUEST", 422)
        try:
            return hooks.load_result(run_id)
        except FileNotFoundError:
            raise PortError("NOT_FOUND", 404)

    return router
