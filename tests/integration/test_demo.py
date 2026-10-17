from fastapi.testclient import TestClient
from app.main import create_app
from app.api.config import Settings
from app.adapters.demo import demo_ports
from app.runtime.health import HealthProbeResult, utcnow

TOKEN='demo-integration-token-000000000000000'
def client_for(target_healthy=True):
    s=Settings(mode='demo',operator_token=TOKEN,context='local-demo',namespace='local-http',sandbox_id='local-demo')
    p=demo_ports(s)
    async def probe(): return HealthProbeResult(status='healthy' if target_healthy else 'unavailable', requests=42, probed_at=utcnow())
    p.runtime.guard.health.probe_fresh=probe
    return TestClient(create_app(s,p),headers={'Authorization':'Bearer '+TOKEN})

def test_injection_is_blocked_before_mutation_with_error_popup_payload():
    with client_for() as c:
        r=c.post('/api/v1/demo/injection',json={'fault_type':'service_pause','duration_seconds':90})
        assert r.status_code==200
        result=r.json()
        assert result['rule_id']=='DURATION_HARD_LIMIT'
        assert result['decision']=='BLOCK' and result['executed'] is False
        assert result['mutations_dispatched']==0 and result['target_status']=='healthy'
        assert 'Error has occurred' in result['message'] and result['stop_latched'] is True
        assert c.get('/api/v1/demo/status').json()==result
        assert c.post('/api/v1/demo/injection',json={'fault_type':'service_pause','duration_seconds':30}).json()['rule_id']=='STOP_LATCHED'
        assert c.post('/api/v1/safety/reset',json={}).status_code==200

def test_missing_target_prevents_execution_and_reset():
    with client_for(False) as c:
        r=c.post('/api/v1/demo/injection',json={'fault_type':'service_pause','duration_seconds':90}).json()
        assert r['rule_id']=='TARGET_UNAVAILABLE' and r['executed'] is False
        assert c.post('/api/v1/safety/reset',json={}).status_code==409

def test_even_within_limit_demo_has_no_execution_authority():
    with client_for() as c:
        result = c.post('/api/v1/demo/injection',json={'fault_type':'service_pause','duration_seconds':30}).json()
        assert result['decision']=='ALLOW' and result['rule_id']=='DURATION_WITHIN_LIMIT'
        assert result['executed'] is False and result['mutations_dispatched']==0
        assert result['stop_latched'] is False
        assert c.post('/api/v1/experiments/x/start',json={'approval_id':'x'},headers={'Idempotency-Key':'x'}).status_code==409
