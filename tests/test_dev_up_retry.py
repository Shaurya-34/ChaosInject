"""scripts/dev.py up: services that died on a slow first start are started once more (found on a cold GitHub Codespace)."""

import argparse
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def dev(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("chaosinject_dev_retry", ROOT / "scripts" / "dev.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "frontend" / "dist").mkdir(parents=True)
    (tmp_path / "frontend" / "dist" / "index.html").write_text("<html></html>")
    (tmp_path / "python").write_text("")
    hotel = tmp_path / "hotel.yml"
    hotel.write_text("")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "VENV_PY", tmp_path / "python")
    monkeypatch.setattr(module, "HOTEL", hotel)
    monkeypatch.setattr(module, "docker_problem", lambda *a, **k: None)
    monkeypatch.setattr(module, "api_state", lambda: None)
    monkeypatch.setattr(module, "start_api", lambda *a, **k: None)
    monkeypatch.setattr(module, "status", lambda *a, **k: None)
    return module


def args():
    return argparse.Namespace(llm=None, llm_base_url=None, execute=False)


def test_a_frontend_that_is_slow_gets_the_exited_services_started_again_once(dev, monkeypatch):
    compose_calls, waits = [], []
    monkeypatch.setattr(dev, "compose", lambda file, *a, **k: compose_calls.append((Path(file).name, a)))

    def wait_for(label, url, seconds):
        waits.append((label, seconds))
        if label == "hotel frontend" and len([w for w in waits if w[0] == label]) == 1:
            raise SystemExit("hotel frontend did not become ready")

    monkeypatch.setattr(dev, "wait_for", wait_for)
    dev.up(args())
    hotel_ups = [a for name, a in compose_calls if name == "hotel.yml" and a[:2] == ("up", "-d")]
    assert len(hotel_ups) == 2 and hotel_ups[1] == ("up", "-d")
    assert [w for w in waits if w[0] == "hotel frontend"] == [("hotel frontend", 120), ("hotel frontend", 240)]


def test_a_frontend_that_is_fast_is_not_retried(dev, monkeypatch):
    compose_calls = []
    monkeypatch.setattr(dev, "compose", lambda file, *a, **k: compose_calls.append((Path(file).name, a)))
    monkeypatch.setattr(dev, "wait_for", lambda *a, **k: None)
    dev.up(args())
    assert len([1 for name, a in compose_calls if name == "hotel.yml" and a[:2] == ("up", "-d")]) == 1


def test_a_frontend_that_never_answers_still_stops_with_the_message(dev, monkeypatch):
    monkeypatch.setattr(dev, "compose", lambda *a, **k: None)

    def wait_for(label, url, seconds):
        raise SystemExit(f"{label} did not become ready within {seconds}s")

    monkeypatch.setattr(dev, "wait_for", wait_for)
    with pytest.raises(SystemExit, match="within 240s"):
        dev.up(args())
