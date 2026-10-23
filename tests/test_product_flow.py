"""The dashboard calls only the chaos API (a source-level guard).

The rendered behaviour is tested in frontend/tests/chaos.spec.ts. This catches a regression the browser tests cannot:
a component calling an endpoint that is not one of the chaos routes the API serves.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "frontend" / "src"
MAIN = (SRC / "main.tsx").read_text(encoding="utf-8")
OLD_ENDPOINTS = ["/health", "/topology", "/state'", "/learning", "/experiments/catalog", "/experiments/plan", "/demo/", "/emergency-stop",
                 "/safety/", "/actions", "/runs/fixture-run"]


def sources():
    return [p for p in SRC.glob("*") if p.suffix in (".ts", ".tsx")]


def test_no_source_calls_an_endpoint_the_api_no_longer_has():
    for path in sources():
        text = path.read_text(encoding="utf-8")
        for endpoint in OLD_ENDPOINTS:
            assert endpoint not in text, f"{path.name} reaches {endpoint}"
        assert "LEGACY" not in text and "./generated" not in text, path.name


def test_the_only_fetch_goes_through_the_chaos_client():
    assert [p.name for p in sources() if re.search(r"\bfetch\(", p.read_text(encoding="utf-8"))] == ["chaosApi.ts"]
    assert "/api/v1/chaos" in (SRC / "chaosApi.ts").read_text(encoding="utf-8")


def test_connecting_checks_the_environment_route():
    assert "await chaos('/environment')" in MAIN


def test_emergency_stop_calls_the_chaos_stop_route():
    assert "chaos('/active/stop','POST')" in MAIN
    assert "disabled={!connected} onClick={()=>action(stop)}" in MAIN  # visible always, enabled once connected


def test_the_product_never_says_fixture_or_synthetic():
    for path in sources():
        assert not re.search(r"fixture|synthetic", path.read_text(encoding="utf-8"), re.I) or path.name == "chaosApi.ts", path.name
