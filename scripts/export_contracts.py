"""Generate schemas and OpenAPI deterministically; use --check to reject drift."""
import inspect
import json
import sys
from pathlib import Path
from app.contracts import models
from app.main import create_app
from app.api.config import Settings
from app.api.ports import Ports
from app.adapters.replay import ReplayBundle

root=Path(__file__).resolve().parents[1]
emitted=set()
def emit(path,value):
    emitted.add(path.name)
    content=json.dumps(value,indent=2,sort_keys=True,allow_nan=False)+"\n"
    if "--check" in sys.argv:
        if not path.exists() or path.read_text(encoding="utf-8")!=content: raise SystemExit(f"Contract drift: {path.name}")
    else: path.write_text(content,encoding="utf-8")
for name, cls in inspect.getmembers(models,inspect.isclass):
    if issubclass(cls,models.Contract): emit(root/"contracts/v1"/(name+".schema.json"),cls.model_json_schema())
emit(root/"contracts/v1/ReplayBundle.schema.json",ReplayBundle.model_json_schema())
# Export doesn't instantiate fixture dependencies or need secrets/cluster.
app=create_app(Settings(operator_token="schema-generation-token-0000000000"),Ports(None,None,None,None))
openapi=app.openapi()
for name,cls in inspect.getmembers(models,inspect.isclass):
    if issubclass(cls,models.Contract):
        schema=cls.model_json_schema(ref_template="#/components/schemas/{model}")
        openapi["components"]["schemas"].update(schema.pop("$defs",{}))
        openapi["components"]["schemas"][name]=schema
emit(root/"contracts/v1/openapi.json",openapi)
# A schema whose model was removed must not linger: delete it (or fail --check).
for stale in sorted((root/"contracts/v1").glob("*.schema.json")):
    if stale.name not in emitted:
        if "--check" in sys.argv: raise SystemExit(f"Contract drift: stale schema {stale.name}")
        stale.unlink()
