"""Rate limits (429/5xx back off instead of hammering the API) and the per-request cap."""

import json

import httpx
import pytest

from app.intelligence import llm_transport as lt, monitoring_agent

from .test_llm_transport import DEP_MAP, GROQ, KEY, completion, explanations_reply, findings, live, mock, record


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(lt, "_clock", lambda: now[0])
    lt.reset_backoff()
    yield now
    lt.reset_backoff()


def counting(status=200, headers=None, text=None):
    calls = []

    def handler(request):
        calls.append(1)
        if status == 200:
            return httpx.Response(200, json=completion(text or "{}"))
        return httpx.Response(status, headers=headers or {}, text="x")

    return handler, calls


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_a_429_or_5xx_starts_a_backoff_and_the_next_calls_send_nothing(clock, status):
    handler, calls = counting(status)
    t = lt.make_transport(GROQ, "m", KEY, http_transport=mock(handler))
    with pytest.raises(lt.LLMError, match=f"HTTP {status}"):
        t("s", "u", 10, 5.0)
    for _ in range(3):
        with pytest.raises(lt.LLMError, match="backing off"):
            t("s", "u", 10, 5.0)
    assert len(calls) == 1
    clock[0] += lt.BACKOFF_S + 1
    with pytest.raises(lt.LLMError, match=f"HTTP {status}"):
        t("s", "u", 10, 5.0)  # the backoff ended, so one more request is allowed
    assert len(calls) == 2


def test_retry_after_is_honoured_but_capped(clock):
    handler, calls = counting(429, {"retry-after": "75"})
    t = lt.make_transport(GROQ, "m", KEY, http_transport=mock(handler))
    with pytest.raises(lt.LLMError):
        t("s", "u", 10, 5.0)
    clock[0] += 60
    with pytest.raises(lt.LLMError, match="backing off"):
        t("s", "u", 10, 5.0)  # 75 s were asked for and only 60 have passed
    clock[0] += 20
    with pytest.raises(lt.LLMError, match="HTTP 429"):
        t("s", "u", 10, 5.0)
    huge, _ = counting(429, {"retry-after": "99999"})
    lt.reset_backoff()
    with pytest.raises(lt.LLMError):
        lt.make_transport(GROQ, "m", KEY, http_transport=mock(huge))("s", "u", 10, 5.0)
    assert lt._backoff_until["api.groq.com"] - clock[0] == lt.MAX_BACKOFF_S


def test_timeouts_and_unreachable_hosts_back_off_too(clock):
    for exc in (httpx.ReadTimeout("slow"), httpx.ConnectError("refused")):
        lt.reset_backoff()
        calls = []

        def handler(request, exc=exc):
            calls.append(1)
            raise exc

        t = lt.make_transport(GROQ, "m", KEY, http_transport=mock(handler))
        for _ in range(3):
            with pytest.raises(lt.LLMError):
                t("s", "u", 10, 5.0)
        assert len(calls) == 1


def test_other_failures_do_not_start_a_backoff(clock):
    handler, calls = counting(401)
    t = lt.make_transport(GROQ, "m", KEY, http_transport=mock(handler))
    for _ in range(2):
        with pytest.raises(lt.LLMError, match="HTTP 401"):
            t("s", "u", 10, 5.0)
    assert len(calls) == 2


def test_a_rate_limited_model_keeps_the_rule_based_text_and_stops_calling(clock):
    f = findings()
    handler, calls = counting(429)
    provider = live(handler)
    for _ in range(3):
        assert monitoring_agent.enrich_explanations(f, provider) == f
    assert len(calls) == 1  # the second attempt and the later refreshes sent nothing


def test_the_backoff_message_never_carries_the_key(clock):
    handler, _ = counting(429)
    t = lt.make_transport(GROQ, "m", KEY, http_transport=mock(handler))
    errors = []
    for _ in range(2):
        with pytest.raises(lt.LLMError) as e:
            t("s", "u", 10, 5.0)
        errors.append(str(e.value))
    assert all(KEY not in text for text in errors)


def test_at_most_n_findings_go_to_the_model_per_request(monkeypatch):
    runs = [record(target=t, failed=10) for t in ("hotel-sandbox-geo-1", "hotel-sandbox-rate-1", "hotel-sandbox-memcached-rate-1")]
    many = monitoring_agent.analyze(runs, DEP_MAP)["findings"]
    assert len(many) == 3
    monkeypatch.setattr(monitoring_agent, "MAX_ITEMS_PER_CALL", 2)
    sent = []

    def handler(request):
        data = json.loads(json.loads(request.content)["messages"][1]["content"].split("DATA:\n", 1)[1])
        sent.append(len(data))
        return httpx.Response(200, json=completion(explanations_reply(data, lambda _: "Wording.")))

    out = monitoring_agent.enrich_explanations(many, live(handler))
    assert sent == [2] and [f["explanation_source"] for f in out].count("live") == 2
    assert "cap of" in lt.fallback_limitation("live", 3, 2)


# --- end to end through the routes -----------------------------------------------------------------

from .test_llm_transport import client, go_live  # noqa: E402,F401  (fixtures and helper reused)


def test_a_429_through_the_routes_gives_rule_based_text_a_limitation_and_no_repeat_calls(client, monkeypatch, clock):
    handler, calls = counting(429, {"retry-after": "30"})
    go_live(monkeypatch, handler)
    first = client.get("/api/v1/chaos/findings")
    assert first.status_code == 200
    body = first.json()
    assert body["findings"][0]["explanation_source"] == "deterministic"
    assert any("rate-limited" in note for note in body["limitations"])
    for _ in range(3):
        assert client.get("/api/v1/chaos/findings").status_code == 200
        assert client.get("/api/v1/chaos/proposals").status_code == 200
    assert len(calls) == 1  # one request, then the backoff kept every later refresh away from the API
    assert KEY not in first.text
