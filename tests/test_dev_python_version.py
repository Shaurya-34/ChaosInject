"""scripts/dev.py setup: refuse a Python that cannot install requirements.lock, and say which to use."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def dev():
    spec = importlib.util.spec_from_file_location("chaosinject_dev_python", ROOT / "scripts" / "dev.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("version", [(3, 11, 9), (3, 12, 10), (3, 13, 0)])
def test_supported_pythons_have_no_problem(dev, version):
    assert dev.python_problem(version) is None


@pytest.mark.parametrize("version", [(3, 10, 14), (3, 14, 7)])
def test_other_pythons_are_refused_with_what_to_do(dev, version):
    message = dev.python_problem(version)
    assert f"Python {version[0]}.{version[1]} is not supported" in message and "3.11 to 3.13" in message and "py -3.12" in message


def test_setup_stops_before_creating_a_venv_on_an_unsupported_python(dev, monkeypatch, tmp_path):
    monkeypatch.setattr(dev, "VENV_PY", tmp_path / "missing-python")
    monkeypatch.setattr(dev, "python_problem", lambda *a: "Python 3.14 is not supported")
    monkeypatch.setattr(dev, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("nothing may run")))
    with pytest.raises(SystemExit, match="not supported"):
        dev.setup(None)


def test_a_failed_install_removes_the_half_made_venv_so_the_next_try_starts_clean(dev, monkeypatch, tmp_path):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev, "VENV_PY", tmp_path / ".venv" / "python")
    monkeypatch.setattr(dev, "python_problem", lambda *a: None)

    def fake_run(*args, **kw):
        if args[1] == "-m" and args[2] == "venv":
            (tmp_path / ".venv").mkdir()
            (tmp_path / ".venv" / "python").write_text("")
            return
        raise dev.subprocess.CalledProcessError(1, [str(a) for a in args])

    monkeypatch.setattr(dev, "run", fake_run)
    with pytest.raises(SystemExit, match="python3-venv") as exit_info:
        dev.setup(None)
    assert "half-made .venv was removed" in str(exit_info.value) and not (tmp_path / ".venv").exists()


def test_a_failure_in_an_existing_venv_is_reported_without_deleting_it(dev, monkeypatch, tmp_path):
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "python").write_text("")
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev, "VENV_PY", tmp_path / ".venv" / "python")
    monkeypatch.setattr(dev, "python_problem", lambda *a: None)
    monkeypatch.setattr(dev, "run", lambda *a, **k: (_ for _ in ()).throw(dev.subprocess.CalledProcessError(1, [str(x) for x in a])))
    with pytest.raises(SystemExit, match="Could not set up"):
        dev.setup(None)
    assert (tmp_path / ".venv").exists()


def test_the_readme_names_the_supported_pythons():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Python 3.11 to 3.13" in text and "3.14 does not work yet" in text
