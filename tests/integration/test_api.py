import asyncio
from datetime import timedelta
import json
import time
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.api.config import Settings,configured_ports
from app.api.errors import PortError
from app.adapters.fixtures import fixture_ports,now,load
from app.contracts import *

TOKEN='integration-test-token-000000000000000'
@pytest.fixture
def client():
    app=create_app(Settings(operator_token=TOKEN,fake_runtime_enabled=True),fixture_ports(True,delay=.05))
    with TestClient(app,headers={'Authorization':'Bearer '+TOKEN}) as c: yield c

def planned(c):
    p=c.post('/api/v1/experiments/plan',json={}).json()[0]
    v=c.post('/api/v1/experiments/validate',json=p).json()
    return p,v

def test_auth_cors_body_limit_and_safe_errors(client):
    assert client.get('/api/v1/state',headers={'Authorization':'bad'}).status_code==401
    assert client.post('/api/v1/emergency-stop',headers={'Origin':'https://evil.example'},json={}).status_code==403
    assert client.post('/api/v1/emergency-stop',content=b'x'*65537).status_code==413
    secret='PRIVATE_LITERAL'
    r=client.post('/api/v1/experiments/x/approve',json={'proposal_hash':'0'*64,'unexpected':secret})
    assert r.status_code==422 and secret not in r.text
    assert 'correlation_id' in r.json()

def test_default_fixture_mutations_disabled():
    with TestClient(create_app(Settings(operator_token=TOKEN),fixture_ports()),headers={'Authorization':'Bearer '+TOKEN}) as c:
        assert c.post('/api/v1/experiments/x/start',json={'approval_id':'x'},headers={'Idempotency-Key':'test'}).status_code==409

def test_exact_approval_idempotent_start_and_pending_stop(client):
    p,v=planned(client)
    bad=client.post(f"/api/v1/experiments/{p['proposal_id']}/approve",json={'proposal_hash':'0'*64})
    assert bad.status_code==409
    a=client.post(f"/api/v1/experiments/{p['proposal_id']}/approve",json={'proposal_hash':v['proposal_hash']}).json()
    path=f"/api/v1/experiments/{p['proposal_id']}/start"
    r=client.post(path,json={'approval_id':a['approval_id']},headers={'Idempotency-Key':'unique-test'})
    assert r.status_code==202
    rid=r.json()['run_id']
    retry=client.post(path,json={'approval_id':a['approval_id']},headers={'Idempotency-Key':'unique-test'})
    assert retry.json()['run_id']==rid
    consumed=client.post(path,json={'approval_id':a['approval_id']},headers={'Idempotency-Key':'new-test'})
    assert consumed.status_code==409
    receipt=client.post('/api/v1/runs/'+rid+'/stop',json={})
    assert receipt.status_code==202 and receipt.json()['cleanup_status']=='pending'
    assert 'recovery_status' not in receipt.json()
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        status=client.get('/api/v1/runs/'+rid).json()
        if status['summary']: break
        time.sleep(.03)
    assert status['cleanup_status']=='verified'
    events=client.get('/api/v1/runs/'+rid+'/events?after_sequence=1').json()
    assert all(e['sequence']>1 for e in events) and len(events)<=128

def test_expiry_stop_latch_and_reconciliation(client):
    p,v=planned(client)
    runtime=client.app.state.ports.runtime
    runtime.proposals[p['proposal_id']].expires_at=now()-timedelta(seconds=1)
    r=client.post(f"/api/v1/experiments/{p['proposal_id']}/approve",json={'proposal_hash':v['proposal_hash']})
    assert r.json()['code']=='EXPIRED'
    assert client.post('/api/v1/emergency-stop',json={}).json()['stop_latched']
    assert not client.post('/api/v1/experiments/validate',json=p).json()['valid']
    assert client.post('/api/v1/safety/reset',json={}).status_code==200

def test_reports_values(client):
    r=client.get('/api/v1/reports/fixture-run')
    assert r.status_code==200
    s=load('summary',RunSummary)
    assert s.evidence_hash in r.text and '580.0' in r.text and 'fixture' in r.text

def test_live_missing_modules_fails_closed(monkeypatch):
    monkeypatch.delenv('LIVE_PORTS_FACTORY',raising=False)
    with pytest.raises(ValueError,match='no fixture fallback'): configured_ports(Settings(mode='live',operator_token=TOKEN))
    with pytest.raises(ValueError): Settings(mode='live',operator_token=TOKEN,fake_runtime_enabled=True)

def test_provenance_mismatch_refused(client):
    async def wrong(sandbox_id): return load('state',SystemStateSnapshot).model_copy(update={'source':'live'})
    client.app.state.ports.telemetry.get_state=wrong
    assert client.get('/api/v1/state').status_code==503

def test_dependency_exception_does_not_echo_private_data(client,caplog):
    async def broken(*args): raise RuntimeError('PRIVATE_INPUT_MUST_NOT_ESCAPE')
    client.app.state.ports.intelligence.analyze_run=broken
    r=client.get('/api/v1/runs/fixture-run/analysis')
    assert r.status_code==503 and 'PRIVATE_INPUT' not in r.text
    assert 'PRIVATE_INPUT' not in caplog.text

def test_actions_disabled(client):
    assert client.post('/api/v1/actions/propose',json={}).json()['code']=='ACTIONS_DISABLED'

def test_health_in_fixture_and_demo_modes(client):
    r=client.get('/api/v1/health')
    assert r.status_code==200 and r.json()['status']=='ready' and 'sql_execution_enabled' not in r.json()
