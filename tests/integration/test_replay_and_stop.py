import json
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.api.config import Settings
from app.adapters.fixtures import fixture_ports,load
from app.adapters.replay import replay_ports
from app.api.evidence import summary_hash
from app.contracts import *

TOKEN='test-replay-token-0000000000000000000'
def test_stop_does_not_need_intelligence_model():
    ports=fixture_ports()
    ports.intelligence=None
    with TestClient(create_app(Settings(operator_token=TOKEN),ports),headers={'Authorization':'Bearer '+TOKEN}) as c:
        assert c.post('/api/v1/emergency-stop',json={}).status_code==202

def test_reset_refuses_failed_cleanup():
    ports=fixture_ports()
    async def failed(sandbox_id): return CleanupSummary(source='fixture',sandbox_id=sandbox_id,cleanup_status='failed',recovery_status='unknown',unresolved_run_ids=['unresolved'])
    ports.runtime.reconcile=failed
    with TestClient(create_app(Settings(operator_token=TOKEN),ports),headers={'Authorization':'Bearer '+TOKEN}) as c:
        assert c.post('/api/v1/safety/reset',json={}).status_code==409

def test_replay_is_read_only_and_rejects_hash_tamper(tmp_path):
    # Test-only synthetic replay fixture, never distributed as authentic evidence.
    s=load('summary',RunSummary).model_copy(update={'source':'replay'})
    s=s.model_copy(update={'evidence_hash':summary_hash(s)})
    a=load('analysis',AnalysisResult).model_copy(deep=True,update={'source':'replay'})
    for ref in a.evidence_refs+[r for f in a.hypotheses+a.recommendations for r in f.evidence_refs]: ref.evidence_hash=s.evidence_hash
    b={'schema_version':'1.0','recorded_from':'live','summary':s.model_dump(mode='json'),'analysis':a.model_dump(mode='json'),'topology':load('topology',TopologySnapshot).model_copy(update={'source':'replay'}).model_dump(mode='json'),'state':load('state',SystemStateSnapshot).model_copy(update={'source':'replay'}).model_dump(mode='json'),'learning':load('learning',LearningStatus).model_copy(update={'source':'replay'}).model_dump(mode='json'),'events':[]}
    path=tmp_path/'replay.json';path.write_text(json.dumps(b),encoding='utf-8')
    ports=replay_ports(path)
    with TestClient(create_app(Settings(mode='replay',operator_token=TOKEN),ports),headers={'Authorization':'Bearer '+TOKEN}) as c:
        assert c.get('/api/v1/runs/fixture-run').json()['source']=='replay'
        assert c.post('/api/v1/emergency-stop',json={}).status_code==409
        assert c.post('/api/v1/sql/analyze',json={'sql':'SELECT 1'}).status_code==409
        assert c.get('/api/v1/reports/fixture-run').status_code==200
    b['summary']['recovery_seconds']=99;path.write_text(json.dumps(b),encoding='utf-8')
    with pytest.raises(Exception): replay_ports(path)
