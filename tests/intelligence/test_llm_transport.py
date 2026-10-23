"""Language-model transport: OpenAI-compatible calls, fallbacks, cache, key handling, dev.py checks. No network."""

import importlib.util
import json
import logging
import urllib.error
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import chaos_runs
from app.api.config import Settings
from app.intelligence import injection_agent, llm_transport as lt, monitoring_agent
from app.intelligence.reasoning import DeterministicProvider, LiveProvider, ReasoningBudget, provider_from_env
from app.main import create_app
from chaos import hooks

from .test_monitoring_agent import DEP_MAP, record

ROOT = Path(__file__).resolve().parents[2]
POLICY = json.loads((ROOT / "chaos" / "policy.json").read_text(encoding="utf-8"))
KEY = "gsk_SUPER_SECRET_KEY_123"
GROQ = "https://api.groq.com/openai/v1"


def completion(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def mock(handler):
    return httpx.MockTransport(handler)


def findings(**kw):
    return monitoring_agent.analyze([record(failed=10, **kw)], DEP_MAP)["findings"]


def proposals(n=2):
    return injection_agent.propose(DEP_MAP, [], POLICY, config=injection_agent.AgentConfig(top_n=n))["proposals"]


def live(handler, model="llama-3.3-70b-versatile", base=GROQ):
    transport = lt.make_transport(base, model, KEY, http_transport=mock(handler))
    return LiveProvider(transport=transport, model_id=model, location=lt.endpoint_location(base))


def explanations_reply(items, text_for):
    return json.dumps({"explanations": [{"id": f["id"], "text": text_for(f)} for f in items]})


# --- the transport ------------------------------------------------------------------------------


def test_request_follows_the_openai_chat_completions_shape():
    seen = {}

    def handler(request):
        seen["url"], seen["auth"] = str(request.url), request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion('{"ok": true}'))

    out = lt.make_transport(GROQ, "m1", KEY, http_transport=mock(handler))("SYS", "USER", 321, 5.0)
    assert out == '{"ok": true}'
    assert seen["url"] == GROQ + "/chat/completions" and seen["auth"] == "Bearer " + KEY
    body = seen["body"]
    assert body["model"] == "m1" and body["max_tokens"] == 321 and body["temperature"] == 0 and body["stream"] is False
    assert body["messages"] == [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}]
    assert body["response_format"] == {"type": "json_object"}


def test_no_authorization_header_without_a_key_for_a_local_server():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=completion("{}"))

    lt.make_transport("http://127.0.0.1:11434/v1", "qwen2.5:7b", "", http_transport=mock(handler))("s", "u", 10, 5.0)
    assert seen["auth"] is None


def test_code_fences_around_the_json_are_stripped():
    t = lt.make_transport(GROQ, "m", KEY, http_transport=mock(lambda r: httpx.Response(200, json=completion('```json\n{"a": 1}\n```'))))
    assert json.loads(t("s", "u", 10, 5.0)) == {"a": 1}


def test_json_mode_is_dropped_and_retried_when_the_host_rejects_it():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(400, json={"error": "response_format unsupported"}) if "response_format" in bodies[-1] \
            else httpx.Response(200, json=completion("{}"))

    assert lt.make_transport(GROQ, "m", KEY, http_transport=mock(handler))("s", "u", 10, 5.0) == "{}"
    assert len(bodies) == 2 and "response_format" not in bodies[1]


def test_reasoning_effort_is_sent_only_when_set_and_dropped_if_the_host_rejects_it():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(400, json={"error": "unknown parameter"}) if "reasoning_effort" in bodies[-1] \
            else httpx.Response(200, json=completion("{}"))

    lt.make_transport(GROQ, "m", KEY, http_transport=mock(handler))("s", "u", 10, 5.0)
    assert "reasoning_effort" not in bodies[0]
    bodies.clear()
    lt.make_transport(GROQ, "openai/gpt-oss-20b", KEY, http_transport=mock(handler), reasoning_effort="low")("s", "u", 10, 5.0)
    assert bodies[0]["reasoning_effort"] == "low"
    # the host rejected reasoning_effort: JSON mode is dropped first, then the effort, one field at a time
    assert len(bodies) == 3
    assert "response_format" not in bodies[1] and bodies[1]["reasoning_effort"] == "low"
    assert "response_format" not in bodies[2] and "reasoning_effort" not in bodies[2]


def test_a_host_that_only_rejects_json_mode_still_gets_the_reasoning_effort():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(400, json={"error": "response_format unsupported"}) if "response_format" in bodies[-1] \
            else httpx.Response(200, json=completion("{}"))

    out = lt.make_transport(GROQ, "openai/gpt-oss-20b", KEY, http_transport=mock(handler), reasoning_effort="low")("s", "u", 10, 5.0)
    assert out == "{}" and len(bodies) == 2
    assert "response_format" not in bodies[1] and bodies[1]["reasoning_effort"] == "low"


def test_a_persistent_400_stops_after_dropping_both_optional_fields():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(400, json={"error": "bad"})

    with pytest.raises(lt.LLMError, match="HTTP 400"):
        lt.make_transport(GROQ, "m", KEY, http_transport=mock(handler), reasoning_effort="low")("s", "u", 10, 5.0)
    assert len(calls) == 3 and "response_format" not in calls[2] and "reasoning_effort" not in calls[2]


@pytest.mark.parametrize("value", ["none", "low", "medium", "high", "default", "LOW", " High "])
def test_the_reasoning_effort_values_models_take_are_accepted(monkeypatch, value):
    monkeypatch.setenv("LLM_BASE_URL", GROQ)
    monkeypatch.setenv("LLM_API_KEY", KEY)
    monkeypatch.setenv("LLM_REASONING_EFFORT", value)
    assert lt.transport_from_env()[0] is not None


def test_reasoning_effort_comes_from_the_environment_and_is_validated(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", GROQ)
    monkeypatch.setenv("LLM_API_KEY", KEY)
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    assert lt.transport_from_env()[0] is not None
    monkeypatch.setenv("LLM_REASONING_EFFORT", "extreme")
    transport, _, notes = lt.transport_from_env()
    assert transport is None and "LLM_REASONING_EFFORT" in notes[0] and KEY not in notes[0]


@pytest.mark.parametrize("handler,needle", [
    (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), "timed out"),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")), "unreachable"),
    (lambda r: httpx.Response(500, text="boom"), "HTTP 500"),
    (lambda r: httpx.Response(401, json={"error": {"message": f"Invalid API key {KEY}"}}), "HTTP 401"),
    (lambda r: httpx.Response(200, text="not json"), "unexpected response shape"),
    (lambda r: httpx.Response(200, json={"choices": []}), "unexpected response shape"),
    (lambda r: httpx.Response(200, json=completion("   ")), "no text"),
])
def test_failures_raise_a_sanitised_error_that_never_contains_the_key(handler, needle, caplog):
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(lt.LLMError) as e:
            lt.make_transport(GROQ, "m", KEY, http_transport=mock(handler))("s", "u", 10, 5.0)
    assert needle in str(e.value)
    assert KEY not in str(e.value) and KEY not in repr(e.value) and KEY not in caplog.text


def test_redirects_are_not_followed():
    def handler(request):
        return httpx.Response(307, headers={"location": "https://evil.example/steal"})

    with pytest.raises(lt.LLMError, match="HTTP 307"):
        lt.make_transport(GROQ, "m", KEY, http_transport=mock(handler))("s", "u", 10, 5.0)


# --- configuration from the environment --------------------------------------------------------


@pytest.fixture
def env(monkeypatch):
    for name in ("INTELLIGENCE_REASONING_MODE", "INTELLIGENCE_LIVE_MODEL", "LLM_BASE_URL", "LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_there_is_no_default_endpoint(env):
    transport, location, notes = lt.transport_from_env()
    assert transport is None and location is None and "LLM_BASE_URL is not set" in notes[0] and "groq.com" in notes[0]


def test_a_local_ollama_endpoint_needs_no_key(env):
    env.setenv("LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    transport, location, notes = lt.transport_from_env()
    assert callable(transport) and location == "local" and notes == []


def test_a_hosted_endpoint_needs_a_key_and_https(env):
    env.setenv("LLM_BASE_URL", GROQ)
    transport, _, notes = lt.transport_from_env()
    assert transport is None and "LLM_API_KEY is not set for api.groq.com" in notes[0]
    env.setenv("LLM_API_KEY", KEY)
    transport, location, notes = lt.transport_from_env()
    assert callable(transport) and location == "hosted" and notes == []
    env.setenv("LLM_BASE_URL", "http://api.groq.com/openai/v1")
    assert "must use https" in lt.transport_from_env()[2][0]
    env.setenv("LLM_BASE_URL", "not a url")
    assert "not a valid" in lt.transport_from_env()[2][0]


def test_notes_never_contain_the_key(env):
    env.setenv("LLM_API_KEY", KEY)
    for url in ("http://api.groq.com/x", "not a url", GROQ):
        env.setenv("LLM_BASE_URL", url)
        assert all(KEY not in n for n in lt.transport_from_env()[2])


def test_provider_from_env_builds_a_live_provider_only_when_it_can(env):
    assert provider_from_env()[0].mode == "deterministic"
    env.setenv("INTELLIGENCE_REASONING_MODE", "live")
    provider, notes = provider_from_env()  # no model
    assert provider.mode == "deterministic" and "INTELLIGENCE_LIVE_MODEL" in notes[0]
    env.setenv("INTELLIGENCE_LIVE_MODEL", "llama-3.3-70b-versatile")
    provider, notes = provider_from_env()  # no base URL: there is no default
    assert provider.mode == "deterministic" and "LLM_BASE_URL is not set" in notes[0]
    env.setenv("LLM_BASE_URL", GROQ)
    provider, notes = provider_from_env()  # hosted, no key
    assert provider.mode == "deterministic" and "LLM_API_KEY" in notes[0]
    env.setenv("LLM_API_KEY", KEY)
    provider, notes = provider_from_env()
    assert provider.mode == "live" and provider.model_id == "llama-3.3-70b-versatile" and provider.location == "hosted" and notes == []
    assert KEY not in repr(provider)  # the key lives only inside the transport closure


# --- explanations: model text, guards, fallback, cache -----------------------------------------


def test_a_valid_reply_replaces_the_explanation_and_names_the_model(tmp_path):
    f = findings()
    text = "Every probe failed while the fault was active, but the system came back afterwards."
    out = monitoring_agent.enrich_explanations(f, live(lambda r: httpx.Response(200, json=completion(explanations_reply(f, lambda _: text)))))
    assert out[0]["explanation"] == text and out[0]["explanation_source"] == "live"
    assert out[0]["explanation_model"] == "llama-3.3-70b-versatile"
    assert out[0]["severity"] == f[0]["severity"] and out[0]["suggested_fix"] == f[0]["suggested_fix"] and out[0]["evidence"] == f[0]["evidence"]


def test_an_invented_number_is_rejected_and_the_rule_based_text_stays():
    f = findings()
    out = monitoring_agent.enrich_explanations(f, live(lambda r: httpx.Response(200, json=completion(explanations_reply(f, lambda _: "It cost 4242 users their session.")))))
    assert out == f and out[0]["explanation_source"] == "deterministic"


@pytest.mark.parametrize("reply", ['{"wrong": []}', "plain words", '{"explanations": [{"id": "other", "text": "x"}]}'])
def test_a_bad_json_shape_falls_back(reply):
    f = findings()
    assert monitoring_agent.enrich_explanations(f, live(lambda r: httpx.Response(200, json=completion(reply)))) == f


def test_timeouts_and_outages_fall_back():
    f = findings()
    for handler in (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), lambda r: httpx.Response(503, text="down")):
        assert monitoring_agent.enrich_explanations(f, live(handler), ReasoningBudget(max_attempts=1)) == f


def test_a_repeat_call_with_the_same_data_is_served_from_the_cache(tmp_path):
    f = findings()
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=completion(explanations_reply(f, lambda _: "Cached wording for this finding.")))

    cache = lt.ExplanationCache(tmp_path / ".llm_cache")
    provider = live(handler)
    first = monitoring_agent.enrich_explanations(f, provider, cache=cache)
    second = monitoring_agent.enrich_explanations(f, provider, cache=cache)
    assert len(calls) == 1 and first == second and second[0]["explanation"] == "Cached wording for this finding."
    files = list((tmp_path / ".llm_cache").glob("*.json"))
    assert len(files) == 1
    stored = json.loads(files[0].read_text(encoding="utf-8"))
    assert set(stored) == {"text", "model", "created"} and KEY not in files[0].read_text(encoding="utf-8")


def test_the_cache_key_changes_with_the_model_and_with_the_data(tmp_path):
    f = findings()
    cache = lt.ExplanationCache(tmp_path)
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(200, json=completion(explanations_reply(f, lambda _: "Wording.")))

    monitoring_agent.enrich_explanations(f, live(handler, model="model-a"), cache=cache)
    monitoring_agent.enrich_explanations(f, live(handler, model="model-b"), cache=cache)
    other = monitoring_agent.analyze([record(failed=10, recovered=False, recovery_s=None)], DEP_MAP)["findings"]
    monitoring_agent.enrich_explanations(other, live(handler, model="model-a"), cache=cache)
    assert calls == ["model-a", "model-b", "model-a"]


def test_a_cached_text_that_fails_the_number_check_is_not_used(tmp_path):
    f = findings()
    cache = lt.ExplanationCache(tmp_path)
    model = "llama-3.3-70b-versatile"
    item = {"id": f[0]["id"], "severity": f[0]["severity_label"], "facts": f[0]["explanation"], "evidence": f[0]["evidence"]}
    cache.put(lt.cache_key(model, "explanation", item), "It affected 9999 users.", model)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=completion(explanations_reply(f, lambda _: "A clean explanation.")))

    out = monitoring_agent.enrich_explanations(f, live(handler), cache=cache)
    assert len(calls) == 1 and out[0]["explanation"] == "A clean explanation."


def test_only_uncached_findings_are_sent_to_the_model(tmp_path):
    a = monitoring_agent.analyze([record(failed=10), record(target="hotel-sandbox-rate-1", fault="network_delay", during=1500.0)], DEP_MAP)["findings"]
    assert len(a) == 2
    cache = lt.ExplanationCache(tmp_path)
    sent = []

    def handler(request):
        data = json.loads(json.loads(request.content)["messages"][1]["content"].split("DATA:\n", 1)[1])
        sent.append(sorted(i["id"] for i in data))
        return httpx.Response(200, json=completion(explanations_reply([f for f in a if f["id"] in sent[-1]], lambda _: "Wording.")))

    provider = live(handler)
    monitoring_agent.enrich_explanations(a[:1], provider, cache=cache)
    monitoring_agent.enrich_explanations(a, provider, cache=cache)
    assert sent == [[a[0]["id"]], [a[1]["id"]]]


def test_the_model_payload_holds_only_run_data(tmp_path):
    f = findings()
    payloads = []

    def handler(request):
        payloads.append(request.content.decode("utf-8"))
        return httpx.Response(200, json=completion("{}"))

    monitoring_agent.enrich_explanations(f, live(handler), ReasoningBudget(max_attempts=1))
    text = payloads[0]
    assert KEY not in text and "OPERATOR_TOKEN" not in text and "Authorization" not in text and str(ROOT) not in text


def test_rationales_get_the_same_treatment_including_the_cache(tmp_path):
    p = proposals()
    calls = []

    def handler(request):
        calls.append(1)
        reply = {"rationales": [{"id": x["experiment"]["name"], "text": f"Worth running {x['experiment']['fault']} next."} for x in p]}
        return httpx.Response(200, json=completion(json.dumps(reply)))

    cache = lt.ExplanationCache(tmp_path)
    provider = live(handler)
    first = injection_agent.enrich_rationales(p, provider, cache=cache)
    again = injection_agent.enrich_rationales(p, provider, cache=cache)
    assert len(calls) == 1 and first == again
    assert all(x["rationale_source"] == "live" and x["rationale_model"] == "llama-3.3-70b-versatile" for x in first)
    assert [x["score"] for x in first] == [x["score"] for x in p] and [x["experiment"] for x in first] == [x["experiment"] for x in p]
    bad = injection_agent.enrich_rationales(p, live(lambda r: httpx.Response(200, json=completion(json.dumps({"rationales": [{"id": x["experiment"]["name"], "text": "Costs 7777 ms."} for x in p]})))))
    assert bad == p


def test_the_limitation_text_is_only_for_a_live_provider_that_fell_short():
    assert lt.fallback_limitation("deterministic", 3, 0) is None
    assert lt.fallback_limitation("live", 0, 0) is None
    assert lt.fallback_limitation("live", 3, 3) is None
    assert "2 of 3" in lt.fallback_limitation("live", 3, 1)


# --- routes ---------------------------------------------------------------------------------------

TOKEN = "t" * 40


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "RESULTS_DIR", tmp_path)
    hooks.save_result({k: v for k, v in record(failed=10).items() if k not in ("run_id", "saved_at", "summary")})
    with TestClient(create_app(Settings(operator_token=TOKEN)), headers={"Authorization": "Bearer " + TOKEN}) as c:
        yield c


def go_live(monkeypatch, handler):
    monkeypatch.setenv("INTELLIGENCE_REASONING_MODE", "live")
    monkeypatch.setenv("INTELLIGENCE_LIVE_MODEL", "llama-3.3-70b-versatile")
    monkeypatch.setenv("LLM_BASE_URL", GROQ)
    monkeypatch.setenv("LLM_API_KEY", KEY)
    real = lt.make_transport
    monkeypatch.setattr(lt, "make_transport", lambda base, model, key="", http_transport=None, **kw: real(base, model, key, http_transport=mock(handler), **kw))


def test_default_routes_are_rule_based_and_say_so(client, monkeypatch):
    for name in ("INTELLIGENCE_REASONING_MODE", "LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    body = client.get("/api/v1/chaos/findings").json()
    assert body["llm"] == {"mode": "deterministic", "model": None, "location": None} and body["limitations"] == []
    assert all(f["explanation_source"] == "deterministic" for f in body["findings"])
    assert client.get("/api/v1/chaos/proposals").json()["llm"]["mode"] == "deterministic"


def test_live_routes_use_the_model_then_the_cache_and_never_leak_the_key(client, monkeypatch, tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        data = json.loads(json.loads(request.content)["messages"][1]["content"].split("DATA:\n", 1)[1])
        key = "explanations" if "severity" in data[0] else "rationales"
        return httpx.Response(200, json=completion(json.dumps({key: [{"id": i["id"], "text": "Model-written wording."} for i in data]})))

    go_live(monkeypatch, handler)
    first = client.get("/api/v1/chaos/findings").json()
    assert first["llm"] == {"mode": "live", "model": "llama-3.3-70b-versatile", "location": "hosted"} and first["limitations"] == []
    assert first["findings"][0]["explanation"] == "Model-written wording." and first["findings"][0]["explanation_model"] == "llama-3.3-70b-versatile"
    n = len(calls)
    second = client.get("/api/v1/chaos/findings").json()
    assert len(calls) == n and second["findings"] == first["findings"]  # served from results/.llm_cache
    assert (tmp_path / ".llm_cache").is_dir()
    prop = client.get("/api/v1/chaos/proposals").json()
    assert prop["proposals"][0]["rationale_source"] == "live"
    for response in (first, second, prop):
        assert KEY not in json.dumps(response)
    assert "Model-written" in client.get("/api/v1/chaos/findings/report").text
    assert "Explained by llama-3.3-70b-versatile" in client.get("/api/v1/chaos/findings/report").text


def test_an_unreachable_model_gives_a_limitation_not_an_error(client, monkeypatch):
    go_live(monkeypatch, lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))
    r = client.get("/api/v1/chaos/findings")
    assert r.status_code == 200
    body = r.json()
    assert body["findings"][0]["explanation_source"] == "deterministic"
    assert any("gave no usable text" in note for note in body["limitations"])
    assert "Rule-based explanation" in client.get("/api/v1/chaos/findings/report").text
    assert KEY not in r.text


def test_a_missing_key_gives_a_limitation_and_rule_based_text(client, monkeypatch):
    monkeypatch.setenv("INTELLIGENCE_REASONING_MODE", "live")
    monkeypatch.setenv("INTELLIGENCE_LIVE_MODEL", "llama-3.3-70b-versatile")
    monkeypatch.setenv("LLM_BASE_URL", GROQ)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    body = client.get("/api/v1/chaos/findings").json()
    assert body["llm"]["mode"] == "deterministic" and any("LLM_API_KEY is not set" in n for n in body["limitations"])


# --- scripts/dev.py: the endpoint check ---------------------------------------------------------


def load_dev():
    spec = importlib.util.spec_from_file_location("chaosinject_dev", ROOT / "scripts" / "dev.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def opener_for(payload=None, error=None, seen=None):
    def opener(request, timeout=None):
        if seen is not None:
            seen.append((request.full_url, request.get_header("Authorization")))
        if error:
            raise error
        return FakeResponse(payload)
    return opener


def test_dev_local_check_needs_a_running_ollama_with_the_model_pulled():
    dev = load_dev()
    base = "http://127.0.0.1:11434/v1"
    assert dev.check_llm("qwen2.5:7b", base, opener=opener_for({"models": [{"name": "qwen2.5:7b"}]})) == "local"
    assert dev.check_llm("llama3.1", base, opener=opener_for({"models": [{"name": "llama3.1:8b"}]})) == "local"
    with pytest.raises(SystemExit, match="ollama pull qwen2.5:7b"):
        dev.check_llm("qwen2.5:7b", base, opener=opener_for({"models": [{"name": "other:1b"}]}))
    with pytest.raises(SystemExit, match="Install Ollama"):
        dev.check_llm("qwen2.5:7b", base, opener=opener_for(error=urllib.error.URLError("refused")))


def test_dev_hosted_check_sends_its_own_user_agent():
    """Groq's Cloudflare answers urllib's default user-agent with HTTP 403 (error 1010)."""
    dev = load_dev()
    agents = []

    def opener(request, timeout=None):
        agents.append(request.get_header("User-agent"))
        return FakeResponse({"data": [{"id": "m"}]})

    dev.check_llm("m", GROQ, KEY, opener=opener)
    assert agents and agents[0].startswith("chaosinject") and "urllib" not in agents[0].lower()


def test_dev_hosted_check_needs_https_a_key_and_a_model_the_host_offers():
    dev = load_dev()
    seen = []
    ok = opener_for({"data": [{"id": "llama-3.3-70b-versatile"}, {"id": "other"}]}, seen=seen)
    assert dev.check_llm("llama-3.3-70b-versatile", GROQ, KEY, opener=ok) == "hosted"
    assert seen == [(GROQ + "/models", "Bearer " + KEY)]
    with pytest.raises(SystemExit, match="LLM_API_KEY is not set") as e:
        dev.check_llm("m", GROQ, "", opener=ok)
    with pytest.raises(SystemExit, match="https"):
        dev.check_llm("m", "http://api.groq.com/openai/v1", KEY, opener=ok)
    with pytest.raises(SystemExit, match="rejected the API key") as rejected:
        dev.check_llm("m", GROQ, KEY, opener=opener_for(error=urllib.error.HTTPError(GROQ, 401, "no", {}, None)))
    with pytest.raises(SystemExit, match="does not offer the model"):
        dev.check_llm("missing-model", GROQ, KEY, opener=ok)
    with pytest.raises(SystemExit, match="Could not reach"):
        dev.check_llm("m", GROQ, KEY, opener=opener_for(error=urllib.error.URLError("dns")))
    for exit_ in (e, rejected):
        assert KEY not in str(exit_.value)


def test_dev_accepts_the_llm_flags_and_never_takes_a_key_argument():
    dev = load_dev()
    source = (ROOT / "scripts" / "dev.py").read_text(encoding="utf-8")
    assert '"--llm"' in source and '"--llm-base-url"' in source
    assert "--llm-key" not in source and "--api-key" not in source
    assert "LLM_API_KEY" in source and "STATE.write_text" in source
    state_line = source[source.index("STATE.write_text"):source.index("wait_for(\"API + dashboard\"")]
    assert "LLM_API_KEY" not in state_line and "api_key" not in state_line  # the key is never written to .dev/api.json
    assert callable(dev.check_llm)
