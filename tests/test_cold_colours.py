"""The dashboard palette is black, violet and yellow plus the cold hues: no red, orange or pink (hue 320-360 and 0-45).

A static scan of the sources. Warm tints count too (a near-white with a red cast is still warm).
The rendered page is scanned in frontend/tests/chaos.spec.ts.
"""
import colorsys
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "frontend" / "src"
REPORT = Path(__file__).resolve().parents[1] / "backend" / "app" / "intelligence" / "monitoring_agent.py"  # the downloadable HTML report embeds its own CSS
FILES = [*SRC.glob("*.css"), *SRC.glob("*.ts*"),SRC.parent / "index.html", REPORT]
HEX = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
NAMES = re.compile(r"(?<![\w-])(red|orange|pink|crimson|tomato|gold|amber|brown|coral|salmon|maroon|firebrick)(?![\w-])", re.I)


def is_warm(code: str) -> bool:
    h = code.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hue, _, sat = colorsys.rgb_to_hls(r, g, b)
    return sat > 0.12 and (hue * 360 < 45 or hue * 360 >= 320)


def test_there_are_files_to_scan():
    assert any(p.name == "style.css" for p in FILES) and any(p.name == "ChaosRuns.tsx" for p in FILES)


def test_no_warm_hex_colours_in_the_dashboard_sources():
    found = sorted({f"{p.name}: {m.group()}" for p in FILES for m in HEX.finditer(p.read_text(encoding="utf-8")) if is_warm(m.group())})
    assert not found, "warm colours: " + ", ".join(found)


def test_no_warm_colour_names_in_styles():
    found = []
    for p in FILES:
        text = p.read_text(encoding="utf-8")
        if p.suffix == ".css":
            found += [f"{p.name}: {m.group()}" for m in NAMES.finditer(text)]
        else:  # in scripts only look inside quoted colour-ish attributes, not UI words
            found += [f"{p.name}: {m.group()}" for m in re.finditer(r"(?:fill|stroke|color|background)[:=]\s*['\"]?(red|orange|pink|crimson|tomato|gold|brown|coral|salmon|maroon)\b", text, re.I)]
    assert not found, found


def test_emergency_stop_is_a_filled_violet_button():
    css = (SRC / "style.css").read_text(encoding="utf-8")
    rule = re.search(r"(?m)(?:^|\})\s*\.stop\s*\{([^{}]*)\}", css).group(1)
    assert "background:#5f3dc4" in rule and "color:#fff" in rule and "font-weight:800" in rule
