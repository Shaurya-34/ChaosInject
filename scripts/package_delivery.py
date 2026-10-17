"""Source-only publication manifest and runnable source + built UI download."""
from pathlib import Path
import json
import shutil
import zipfile
from app.adapters.fixtures import load
from app.contracts import RunSummary,AnalysisResult
from app.api.evidence import report
root=Path(__file__).resolve().parents[1]
out=root/'outputs'
out.mkdir(exist_ok=True)
(root/'work').mkdir(exist_ok=True)
source_dirs=['backend','contracts','fixtures','prompts','scripts','tests','docs','reports','.github','frontend']
root_files=['README.md','CONTEXT.md','LICENSE','NOTICE','.gitignore','pyproject.toml','requirements.lock']
files=[root/n for n in root_files]
for directory in source_dirs:
    files.extend(p for p in (root/directory).rglob('*') if p.is_file() and not any(part in ['node_modules','dist','__pycache__','test-results','playwright-report'] or part.endswith('.egg-info') for part in p.relative_to(root).parts) and p.suffix not in ['.pyc','.tsbuildinfo'])
groups={}
for p in sorted(files):groups.setdefault(str(p.parent.relative_to(root)).replace('\\','/'),[]).append(str(p))
(root/'work/publication-manifest.json').write_text(json.dumps(groups,indent=2),encoding='utf-8')
with zipfile.ZipFile(out/'AI-Chaos-Pilot-Demo.zip','w',zipfile.ZIP_DEFLATED) as z:
    for p in sorted(files):z.write(p,'AI-Chaos-Pilot/'+p.relative_to(root).as_posix())
    for p in (root/'frontend/dist').rglob('*'):
        if p.is_file():z.write(p,'AI-Chaos-Pilot/'+p.relative_to(root).as_posix())
shutil.copyfile(root/'docs/integration/ARCHITECTURE.html',out/'architecture.html')
shutil.copyfile(root/'docs/integration/TESTING.md',out/'testing-results.md')
shutil.copyfile(root/'docs/integration/TEAM_FORM.md',out/'team-form.md')
fixture_report=report(load('summary',RunSummary),load('analysis',AnalysisResult))
(out/'fixture-findings.html').write_text(fixture_report,encoding='utf-8')
print(f'{len(files)} source files in {len(groups)} publication groups; deliverables packaged.')
