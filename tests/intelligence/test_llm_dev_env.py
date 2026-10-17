"""scripts/dev.py: the git-ignored .env, GROQ_KEY, and keeping the key out of every file and message."""

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
KEY = "gsk_SUPER_SECRET_KEY_123"
GROQ = "https://api.groq.com/openai/v1"


def load_dev():
    spec = importlib.util.spec_from_file_location("chaosinject_dev_env", ROOT / "scripts" / "dev.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_env(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_env_file_parser_handles_spaces_quotes_comments_and_export():
    dev = load_dev()
    text = (
        "# comment\n\nGROQ_KEY = abc123\nQUOTED=\"two words\"\nSINGLE = 'x'\n"
        "export LLM_BASE_URL=https://api.groq.com/openai/v1  # trailing note\nnot a pair\nEMPTY=\n"
    )
    assert dev.parse_env_file(text) == {
        "GROQ_KEY": "abc123", "QUOTED": "two words", "SINGLE": "x",
        "LLM_BASE_URL": "https://api.groq.com/openai/v1", "EMPTY": "",
    }


def test_the_key_comes_from_the_environment_first_then_a_gitignored_env_file(tmp_path):
    dev = load_dev()
    path = write_env(tmp_path, f"GROQ_KEY = {KEY}\nLLM_BASE_URL = {GROQ}\n")
    yes = lambda p: True  # noqa: E731
    assert dev.llm_settings({}, env_path=path, ignored=yes) == {"base_url": GROQ, "key": KEY, "key_source": ".env"}  # GROQ_KEY accepted
    from_env = dev.llm_settings({"LLM_API_KEY": "env-key", "LLM_BASE_URL": "https://other/v1"}, env_path=path, ignored=yes)
    assert from_env == {"base_url": "https://other/v1", "key": "env-key", "key_source": "environment"}
    assert dev.llm_settings({"GROQ_KEY": "g"}, env_path=tmp_path / "missing", ignored=yes)["key"] == "g"
    assert dev.llm_settings({}, base_url_flag="https://flag/v1", env_path=path, ignored=yes)["base_url"] == "https://flag/v1"
    assert dev.llm_settings({}, env_path=tmp_path / "missing")["key"] == ""


def test_an_env_file_that_git_does_not_ignore_is_refused(tmp_path):
    dev = load_dev()
    path = write_env(tmp_path, f"GROQ_KEY={KEY}\n")
    with pytest.raises(SystemExit, match="Refusing to read .env") as e:
        dev.llm_settings({}, env_path=path, ignored=lambda p: False)
    assert KEY not in str(e.value)


def test_the_env_file_is_not_even_read_when_everything_is_in_the_environment(tmp_path):
    dev = load_dev()
    path = write_env(tmp_path, f"GROQ_KEY={KEY}\n")
    asked = []
    out = dev.llm_settings({"LLM_API_KEY": "k", "GROQ_KEY": "k", "LLM_BASE_URL": GROQ}, env_path=path,
                           ignored=lambda p: asked.append(1) or True)
    assert out["key_source"] == "environment" and asked == []


def test_the_repository_ignores_env_but_not_the_example():
    dev = load_dev()
    assert dev.env_file_is_ignored(ROOT / ".env")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "GROQ_KEY=\n" in example and "LLM_BASE_URL=https://api.groq.com/openai/v1" in example
    assert not any(line.split("=", 1)[1].strip() for line in example.splitlines() if line.startswith("GROQ_KEY"))
    assert not dev.env_file_is_ignored(ROOT / ".env.example")


def test_up_with_llm_needs_a_base_url_and_never_defaults_to_ollama(monkeypatch, tmp_path):
    dev = load_dev()
    for name in ("LLM_BASE_URL", "LLM_API_KEY", "GROQ_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(dev, "ENV_FILE", tmp_path / "no.env")
    with pytest.raises(SystemExit, match="needs LLM_BASE_URL"):
        dev.up(argparse.Namespace(llm="some-model", llm_base_url=None, execute=False))


def test_the_key_reaches_only_the_api_process_environment_never_the_state_file(monkeypatch, tmp_path, capsys):
    dev = load_dev()
    seen = {}

    class FakeProcess:
        pid = 4321

    def fake_popen(cmd, **kw):
        seen["env"] = kw["env"]
        return FakeProcess()

    monkeypatch.setattr(dev, "port_in_use", lambda port: False)
    monkeypatch.setattr(dev, "wait_for", lambda *a: None)
    monkeypatch.setattr(dev.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(dev, "STATE", tmp_path / "api.json")
    monkeypatch.setattr(dev, "LOG", tmp_path / "api.log")
    dev.start_api(False, {"model": "m", "base_url": GROQ, "key": KEY, "key_source": ".env"})
    assert seen["env"]["LLM_API_KEY"] == KEY and seen["env"]["INTELLIGENCE_REASONING_MODE"] == "live"
    state_text = (tmp_path / "api.json").read_text(encoding="utf-8")
    assert KEY not in state_text
    assert json.loads(state_text)["llm"] == {"model": "m", "base_url": GROQ, "key_set": True, "key_source": ".env"}
    assert KEY not in capsys.readouterr().out
    for leftover in tmp_path.iterdir():
        assert KEY not in leftover.read_text(encoding="utf-8", errors="ignore")


def test_status_says_the_key_is_set_and_never_shows_it_or_its_length(monkeypatch, capsys):
    dev = load_dev()
    state = {"pid": 1, "token": "operator-token", "execute": False,
             "llm": {"model": "llama-3.3-70b-versatile", "base_url": GROQ, "key_set": True, "key_source": ".env"}}
    monkeypatch.setattr(dev, "api_state", lambda: state)
    monkeypatch.setattr(dev, "stack", lambda *a: "stack")
    monkeypatch.setattr(dev, "http_ok", lambda *a: True)
    monkeypatch.setattr(dev, "read_lock", lambda: None)
    dev.status(None)
    out = capsys.readouterr().out
    assert "LLM llama-3.3-70b-versatile via groq, key set (from .env)" in out
    assert KEY not in out and str(len(KEY)) not in out.replace("operator-token", "")


def test_a_local_endpoint_status_has_no_key_clause(monkeypatch, capsys):
    dev = load_dev()
    state = {"pid": 1, "token": "t", "execute": False,
             "llm": {"model": "qwen2.5:7b", "base_url": "http://127.0.0.1:11434/v1", "key_set": False, "key_source": None}}
    monkeypatch.setattr(dev, "api_state", lambda: state)
    monkeypatch.setattr(dev, "stack", lambda *a: "stack")
    monkeypatch.setattr(dev, "http_ok", lambda *a: True)
    monkeypatch.setattr(dev, "read_lock", lambda: None)
    dev.status(None)
    assert "LLM qwen2.5:7b via 127.0.0.1" in capsys.readouterr().out


def test_the_llm_doc_leads_with_groq_and_states_the_key_and_data_rules():
    doc = (ROOT / "docs" / "llm.md").read_text(encoding="utf-8")
    assert doc.index("Groq") < doc.index("Ollama")
    for needle in (".env.example", "git-ignored", "uncommitted", "LLM_BASE_URL", "no default", "429", "30 s",
                   "never written", "third party", "zero", "file paths", "GROQ_KEY"):
        assert needle in doc, needle
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/llm.md" in readme and "optional LLM" in readme
