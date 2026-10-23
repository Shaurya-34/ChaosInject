"""scripts/dev.py: when Docker cannot be used, say what to do (a faked `docker info`; no Docker needed to run these)."""

import argparse
import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REAL_ERROR = ("failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine; check if the path is correct "
              "and if the daemon is running: open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.")


def load_dev():
    spec = importlib.util.spec_from_file_location("chaosinject_dev_docker", ROOT / "scripts" / "dev.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_missing(cmd, **kw):
    raise FileNotFoundError("docker")


def run_down(cmd, **kw):
    return subprocess.CompletedProcess(cmd, 1, stdout="Client: Docker Engine - Community\n", stderr=REAL_ERROR + "\nsecond line\n")


def run_down_silently(cmd, **kw):
    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")


def run_slow(cmd, **kw):
    raise subprocess.TimeoutExpired(cmd, 20)


def run_ok(cmd, **kw):
    return subprocess.CompletedProcess(cmd, 0, stdout="Server: ok", stderr="")


@pytest.fixture
def dev():
    return load_dev()


def test_a_working_engine_has_no_problem(dev):
    assert dev.docker_problem(run=run_ok, system="Windows") is None


def test_the_cli_missing_says_to_install_docker_per_platform(dev):
    windows = dev.docker_problem(run=run_missing, system="Windows")
    assert "not installed" in windows and "no `docker` command" in windows
    assert "Docker Desktop" in windows and "WSL 2" in windows and "Windows Home" in windows
    assert "Apple Silicon" in dev.docker_problem(run=run_missing, system="Darwin")
    linux = dev.docker_problem(run=run_missing, system="Linux")
    assert "docs.docker.com/engine/install" in linux and "WSL" not in linux


def test_an_engine_that_is_not_running_says_how_to_start_it_and_quotes_docker(dev):
    windows = dev.docker_problem(run=run_down, system="Windows")
    assert "installed but its engine is not running" in windows
    assert "Start Docker Desktop" in windows and "WSL 2" in windows and "wsl --install" in windows and "administrator PowerShell" in windows
    assert "Docker said: failed to connect to the docker API" in windows and "second line" not in windows  # only the first line
    mac = dev.docker_problem(run=run_down, system="Darwin")
    assert "Start Docker Desktop" in mac and "WSL" not in mac
    linux = dev.docker_problem(run=run_down, system="Linux")
    assert "sudo systemctl start docker" in linux and "WSL" not in linux and "Docker Desktop" not in linux


def test_there_is_no_docker_said_clause_when_docker_printed_nothing(dev):
    assert "Docker said" not in dev.docker_problem(run=run_down_silently, system="Windows")


def test_a_slow_engine_asks_for_patience_not_a_traceback(dev):
    message = dev.docker_problem(run=run_slow, system="Windows")
    assert "20 seconds" in message and "still be starting" in message


def test_every_outcome_is_a_plain_string_or_none_and_the_quote_is_bounded(dev):
    long_error = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="x" * 5000)  # noqa: E731
    for runner in (run_missing, run_down, run_down_silently, run_slow, long_error):
        for system in ("Windows", "Darwin", "Linux", "Plan9"):
            message = dev.docker_problem(run=runner, system=system)
            assert isinstance(message, str) and message and "Traceback" not in message and len(message) < 1500


def test_the_platform_defaults_to_the_machine_it_runs_on(dev, monkeypatch):
    monkeypatch.setattr(dev.platform, "system", lambda: "Windows")
    assert "WSL 2" in dev.docker_problem(run=run_down)
    monkeypatch.setattr(dev.platform, "system", lambda: "Linux")
    assert "systemctl" in dev.docker_problem(run=run_down)


def _ready_for_up(dev, monkeypatch, tmp_path):
    """Get `up` past the 'run setup first' check so it reaches the Docker check."""
    (tmp_path / "frontend" / "dist").mkdir(parents=True)
    (tmp_path / "frontend" / "dist" / "index.html").write_text("<html></html>")
    fake_python = tmp_path / "python"
    fake_python.write_text("")
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev, "VENV_PY", fake_python)


def test_up_stops_with_the_message_before_touching_anything(dev, monkeypatch, tmp_path):
    _ready_for_up(dev, monkeypatch, tmp_path)
    monkeypatch.setattr(dev, "docker_problem", lambda *a, **k: "Docker is installed but its engine is not running. Start Docker Desktop.")

    def boom(*a, **k):
        raise AssertionError("nothing may run when Docker is unusable")

    for name in ("run", "compose", "start_api", "wait_for"):
        monkeypatch.setattr(dev, name, boom)
    with pytest.raises(SystemExit) as exit_info:
        dev.up(argparse.Namespace(llm=None, llm_base_url=None, execute=False))
    assert str(exit_info.value) == "Docker is installed but its engine is not running. Start Docker Desktop."


def test_up_still_asks_for_setup_first_when_that_is_what_is_missing(dev, monkeypatch, tmp_path):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev, "VENV_PY", tmp_path / "missing-python")
    monkeypatch.setattr(dev, "docker_problem", lambda *a, **k: "should not be reached")
    with pytest.raises(SystemExit, match="Run `python scripts/dev.py setup` first"):
        dev.up(argparse.Namespace(llm=None, llm_base_url=None, execute=False))


def test_down_without_an_engine_stops_the_dashboard_and_explains_instead_of_crashing(dev, monkeypatch, capsys):
    stopped = []
    monkeypatch.setattr(dev, "read_lock", lambda: None)
    monkeypatch.setattr(dev, "stop_api", lambda: stopped.append(True))
    monkeypatch.setattr(dev, "docker_problem", lambda *a, **k: "Docker is installed but its engine is not running. Start Docker Desktop.")
    monkeypatch.setattr(dev, "compose", lambda *a, **k: (_ for _ in ()).throw(AssertionError("compose must not run")))
    dev.down(argparse.Namespace(force=False))
    out = capsys.readouterr().out
    assert stopped == [True] and "Dashboard stopped" in out and "not touched" in out and "Start Docker Desktop" in out


def test_down_with_a_working_engine_still_takes_the_stacks_down(dev, monkeypatch):
    calls = []
    monkeypatch.setattr(dev, "read_lock", lambda: None)
    monkeypatch.setattr(dev, "stop_api", lambda: None)
    monkeypatch.setattr(dev, "docker_problem", lambda *a, **k: None)
    monkeypatch.setattr(dev, "compose", lambda file, *a, **k: calls.append((Path(file).name, a)))
    dev.down(argparse.Namespace(force=False))
    assert calls and calls[0][1] == ("down",)


def test_status_without_an_engine_does_not_print_misleading_container_counts(dev, monkeypatch, capsys):
    monkeypatch.setattr(dev, "docker_problem", lambda *a, **k: "Docker is installed but its engine is not running, or not reachable. Start Docker Desktop.")
    monkeypatch.setattr(dev, "api_state", lambda: None)
    monkeypatch.setattr(dev, "http_ok", lambda *a, **k: False)
    monkeypatch.setattr(dev, "read_lock", lambda: None)
    monkeypatch.setattr(dev, "stack", lambda *a: (_ for _ in ()).throw(AssertionError("no container counts without an engine")))
    dev.status(None)
    out = capsys.readouterr().out
    assert "docker: not available" in out and "sandbox containers: unknown" in out and "0/0" not in out


def test_the_readme_needs_section_gives_the_disk_and_wsl_figures():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    needs = text[text.index("**Needs:**"):text.index("```bash", text.index("**Needs:**"))]
    for needle in ("WSL 2", "Windows Home", "8 GB", "2 to 5 GB", "2.6 GB", "12 to 15 GB free"):
        assert needle in needs, needle
