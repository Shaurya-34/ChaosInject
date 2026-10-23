import {test,expect} from '@playwright/test';

// These run against the disposable demo server from scripts/browser-test-server.py (a real HTTP target plus the API).
// The hackathon demo page that used to drive this is not part of the product build any more, so the guard is now
// tested where it lives, over HTTP, and the product shell is tested on the same server.
const token='browser-test-disposable-token-0000000000';
const auth={Authorization:'Bearer '+token};
const TARGET='http://127.0.0.1:8290/health';

async function check(request:import('@playwright/test').APIRequestContext,seconds:number){
  const response=await request.post('/api/v1/demo/injection',{headers:auth,data:{fault_type:'service_pause',duration_seconds:seconds}});
  expect(response.ok()).toBeTruthy();
  return response.json();
}

test('the demo guard blocks an unsafe duration before anything is changed and the real target stays healthy',async({request})=>{
  const before=await request.get(TARGET);expect(before.ok()).toBeTruthy();expect((await before.json()).status).toBe('healthy');
  const result=await check(request,90);
  expect(result.rule_id).toBe('DURATION_HARD_LIMIT');expect(result.decision).toBe('BLOCK');
  expect(result.executed).toBe(false);expect(result.mutations_dispatched).toBe(0);
  const after=await (await request.get(TARGET)).json();
  expect(after.status).toBe('healthy');expect(after.mutations_received).toBe(0);
  const safety=await (await request.get('/api/v1/demo/safety',{headers:auth})).json();
  expect(safety.stop_latched).toBe(true);  // a blocked request latches STOP until it is reset
  const reset=await request.post('/api/v1/safety/reset',{headers:auth,data:{}});
  expect(reset.ok()).toBeTruthy();
  expect((await (await request.get('/api/v1/demo/safety',{headers:auth})).json()).stop_latched).toBe(false);
});

test('durations up to 60 s pass the guard and 61 s is blocked, without an outage',async({request})=>{
  for(const seconds of [1,30,60]){
    const result=await check(request,seconds);
    expect(result.duration_seconds).toBe(seconds);expect(result.decision).toBe('ALLOW');expect(result.executed).toBe(false);
    expect(result.stop_latched).toBe(false);expect(result.mutations_dispatched).toBe(0);
  }
  const blocked=await check(request,61);
  expect(blocked.decision).toBe('BLOCK');expect(blocked.stop_latched).toBe(true);
  const target=await (await request.get(TARGET)).json();
  expect(target.status).toBe('healthy');expect(target.mutations_received).toBe(0);
  expect((await request.post('/api/v1/safety/reset',{headers:auth,data:{}})).ok()).toBeTruthy();
});

test('small-screen layout keeps Emergency STOP reachable and the page does not scroll sideways',async({page})=>{
  await page.setViewportSize({width:390,height:844});await page.goto('/');
  await page.getByLabel('Operator token').fill(token);await page.getByRole('button',{name:'Connect workspace'}).click();
  const stop=page.getByRole('button',{name:'Emergency STOP'});
  await expect(stop).toBeVisible();await expect(stop).toBeEnabled();
  const box=await stop.boundingBox();expect(box!.x+box!.width).toBeLessThanOrEqual(390);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth)).toBe(false);
});
