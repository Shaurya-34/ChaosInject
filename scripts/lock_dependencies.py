"""Lock installed project/test dependency closure, excluding audit tool packages."""
import importlib.metadata as md
from pathlib import Path
from packaging.requirements import Requirement
root=Path(__file__).resolve().parents[1]
pending=['fastapi','starlette','pydantic','uvicorn','httpx','pytest','pytest-asyncio','pglast','river']
seen={}
while pending:
    name=pending.pop()
    norm=name.lower().replace('_','-')
    if norm in seen: continue
    dist=md.distribution(name)
    seen[norm]=dist.version
    for raw in dist.requires or []:
        req=Requirement(raw)
        if not req.marker or req.marker.evaluate({'extra':''}): pending.append(req.name)
# Include cross-platform console dependency for Windows reproduction.
seen['colorama']=md.version('colorama')
(root/'requirements.lock').write_text('# Tested project + test dependency closure. Regenerate with scripts/lock_dependencies.py\n'+''.join(f'{n}=={v}\n' for n,v in sorted(seen.items())),encoding='utf-8')
