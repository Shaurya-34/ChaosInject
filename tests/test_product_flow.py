"""The dashboard's product flow must not reach the hackathon pages or their endpoints (source-level guard).

The rendered behaviour is tested in frontend/tests/chaos.spec.ts. This catches a regression the browser tests cannot:
someone un-guarding a legacy page or adding a call to an old endpoint from a product component.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "frontend" / "src"
MAIN = (SRC / "main.tsx").read_text(encoding="utf-8")
PRODUCT_FILES = ["ChaosRuns.tsx", "chaosApi.ts", "Score.tsx", "FirstRun.tsx", "PrometheusCharts.tsx", "metricsUtil.ts"]
OLD_ENDPOINTS = ["/health", "/topology", "/state'", "/learning", "/experiments/catalog", "/experiments/plan", "/demo/", "/emergency-stop",
                 "/safety/", "/actions", "/runs/fixture-run"]


def test_the_legacy_flag_is_off_unless_a_build_turns_it_on():
    assert "VITE_LEGACY_PAGES==='true'" in MAIN
    assert "useState<Page>(LEGACY_PAGES?'System overview':'Chaos runs')" in MAIN  # the product build starts on Chaos runs


def test_every_old_page_render_is_behind_the_flag():
    for name in ("System overview", "Operations", "Findings"):
        before = [MAIN[max(0, m.start() - 14):m.start()] for m in re.finditer(r"page==='%s'&&" % re.escape(name), MAIN)]
        assert before and all(b.endswith("LEGACY_PAGES&&") for b in before), name


def test_the_legacy_endpoints_are_only_read_in_legacy_code_paths():
    assert "if(LEGACY_PAGES)await refresh();else await chaos('/environment')" in MAIN  # connecting uses a chaos route
    assert "useEffect(()=>{if(!connected||!LEGACY_PAGES)return;" in MAIN  # the 1 s polling loop of the old pages
    uses = [m.start() for m in re.finditer(r"refresh\(\)", MAIN) if not MAIN[:m.start()].endswith("function ")]
    # exactly three: connect (guarded above), the guarded polling effect, and the old demo's inject()
    assert len(uses) == 3
    assert "async function inject()" in MAIN[uses[2] - 400:uses[2]] or "inject" in MAIN[uses[2] - 400:uses[2]]


def test_the_provenance_bar_chip_and_pill_are_behind_the_flag():
    for marker in ("<div className={'provenance '", '<span className="source">', '<span className="pill">'):
        i = MAIN.index(marker)
        assert MAIN[max(0, i - 16):i].endswith("LEGACY_PAGES&&"), marker


def test_product_components_never_call_the_old_endpoints_or_say_fixture():
    for name in PRODUCT_FILES:
        text = (SRC / name).read_text(encoding="utf-8")
        for endpoint in OLD_ENDPOINTS:
            assert endpoint not in text, f"{name} reaches {endpoint}"
        assert not re.search(r"fixture|synthetic", text, re.I) or name in ("chaosApi.ts",), name


def test_emergency_stop_calls_the_chaos_stop_route_in_the_product_flow():
    assert "chaos('/active/stop','POST')" in MAIN
    assert "onClick={()=>action(LEGACY_PAGES?legacyStop:productStop)}" in MAIN
    assert "disabled={!connected||(LEGACY_PAGES&&health?.source==='replay')}" in MAIN  # visible always, enabled once connected
