"""Generate the OpenAPI document deterministically; use --check to reject drift."""
import json
import sys
from pathlib import Path
from app.main import create_app
from app.api.config import Settings

root = Path(__file__).resolve().parents[1]
out = root / "contracts/v1/openapi.json"
openapi = create_app(Settings(operator_token="schema-generation-token-0000000000")).openapi()
content = json.dumps(openapi, indent=2, sort_keys=True, allow_nan=False) + "\n"
check = "--check" in sys.argv
if check:
    if not out.exists() or out.read_text(encoding="utf-8") != content: raise SystemExit(f"Contract drift: {out.name}")
else:
    out.write_text(content, encoding="utf-8")
# The per-model schemas of the old API are gone: a leftover one must not linger (delete it, or fail --check).
for stale in sorted((root / "contracts/v1").glob("*.schema.json")):
    if check: raise SystemExit(f"Contract drift: stale schema {stale.name}")
    stale.unlink()
