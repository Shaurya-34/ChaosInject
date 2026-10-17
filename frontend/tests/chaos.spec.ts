import {test,expect,type Page} from '@playwright/test';

// The /api/v1/chaos routes are mocked here: these tests check the dashboard, not the controller.
const token='browser-test-disposable-token-0000000000';
const t0=1_700_000_000;
const sample=(i:number,ok:boolean,latency:number)=>({t:t0+i,ok,status:ok?200:500,latency_ms:latency});
const run={
  run_id:'20261017-000001-hotel_geo_stop-abc123',schema_version:1,saved_at:t0+30,
  experiment:{name:'hotel_geo_stop',target:'hotel-sandbox-geo-1',fault:'container_stop',duration_s:20,probe_url:'http://x',baseline_samples:3,interval_s:1},
  policy:{},baseline:[sample(0,true,40),sample(1,true,42),sample(2,true,41)],
  during:[sample(3,false,3),sample(4,false,3),sample(5,false,4)],recovery:[sample(7,false,5)],
  stop_reason:'safety: error rate 100% over 3 probes',recovered:false,recovery_s:null,fault_started:t0+3,fault_ended:t0+6,
  summary:{baseline:{count:3,failures:0,error_rate:0,p50_ms:41,p95_ms:42,max_ms:42},during:{count:3,failures:3,error_rate:1,p50_ms:null,p95_ms:null,max_ms:null},recovery:{count:1,failures:1,error_rate:1,p50_ms:null,p95_ms:null,max_ms:null}},
};
const index=[{run_id:run.run_id,file:run.run_id+'.json',experiment:'hotel_geo_stop',target:'hotel-sandbox-geo-1',fault:'container_stop',duration_s:20,stop_reason:run.stop_reason,recovered:false,recovery_s:null,during_error_rate:1,during_p95_ms:null,baseline_p95_ms:42}];
const proposals={proposals:[{experiment:{name:'hotel-rate-stop',target:'hotel-sandbox-rate-1',fault:'container_stop',duration_s:20},rationale:'container_stop on rate has never been run; 1 working endpoint(s) depend on rate.',expected_impact:'Stops the container for rate. Requests to /hotels depend on it.',affected_endpoints:['/hotels'],times_run:0,score:0.68,rationale_source:'deterministic'}],rejected:[{target:'hotel-sandbox-frontend-1',fault:'container_stop',reason:"target 'hotel-sandbox-frontend-1' is not allowlisted"}],limitations:[]};
const findings={findings:[{id:'hotel-sandbox-geo-1:container_stop',severity:1,severity_label:'Not recovered / crashed',title:'container_stop on hotel-sandbox-geo-1: not recovered / crashed',target:'hotel-sandbox-geo-1',fault:'container_stop',affected_endpoints:['/hotels'],explanation:'In 1 run(s), the system did not recover after the fault was removed.',suggested_fix:'Run at least two replicas behind service discovery.',run_ids:[run.run_id],explanation_source:'deterministic',evidence:[]}],runs_analyzed:1,runs_without_findings:0,runs_not_evaluated:0,limitations:[]};
const idle={running:false,experiment:null,pid:null,started_at:null,exit_code:null,run_id:null,fault_active:false,fault:null,stop_requested:false,log_tail:[]};

async function mockChaos(page:Page,active:object,onRun?:()=>{status:number,body:object}){
  await page.route('**/api/v1/chaos/**',route=>{
    const url=new URL(route.request().url());const path=url.pathname.replace('/api/v1/chaos','');
    const json=(body:object,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    if(path==='/experiments')return json([{id:'hotel_geo_stop',name:'hotel_geo_stop',target:'hotel-sandbox-geo-1',fault:'container_stop',duration_s:20}]);
    if(path==='/runs')return json(index);
    if(path==='/runs/'+run.run_id)return json(run);
    if(path==='/findings')return json(findings);
    if(path.startsWith('/findings/report'))return route.fulfill({status:200,contentType:'text/markdown',body:'# Chaos findings'});
    if(path==='/proposals')return json(proposals);
    if(path==='/active')return json(active);
    if(path.endsWith('/run')&&onRun){const r=onRun();return json(r.body,r.status);}
    return json({code:'NOT_FOUND',message:'not mocked'},404);
  });
}
async function openChaosRuns(page:Page){
  await page.goto('/');await page.getByLabel('Operator token').fill(token);await page.getByRole('button',{name:'Connect workspace'}).click();
  await page.getByRole('button',{name:'Chaos runs',exact:true}).click();
}

test('runs table highlights unrecovered runs and the detail shows the stop reason',async({page})=>{
  await mockChaos(page,idle);await openChaosRuns(page);
  const row=page.getByRole('row',{name:/hotel_geo_stop/});
  await expect(row).toBeVisible();await expect(row).toContainText('NOT RECOVERED');await expect(row).toHaveClass(/bad/);
  await row.click();
  const detail=page.getByLabel('Run detail');
  await expect(detail).toBeVisible();await expect(detail.getByRole('note')).toContainText('safety: error rate 100% over 3 probes');
  await expect(detail.getByRole('img',{name:/Latency over time/})).toBeVisible();
});

test('Run on a server with execution disabled shows a message and greys the buttons',async({page})=>{
  await mockChaos(page,idle,()=>({status:409,body:{code:'MUTATIONS_DISABLED',message:'Operation refused.'}}));await openChaosRuns(page);
  const run_=page.getByRole('button',{name:'Run',exact:true});
  await run_.click();
  await expect(page.getByRole('status')).toContainText('Execution disabled');await expect(run_).toBeDisabled();
});

test('a leftover fault shows the recover banner',async({page})=>{
  await mockChaos(page,{...idle,fault_active:true,fault:{target:'hotel-sandbox-geo-1',fault:'container_stop'}});await openChaosRuns(page);
  const banner=page.getByRole('alert').filter({hasText:'A fault is still in place'});
  await expect(banner).toBeVisible();await expect(banner.getByRole('button',{name:'Recover'})).toBeEnabled();
});

test('a running experiment shows the live panel with a Stop button',async({page})=>{
  await mockChaos(page,{...idle,running:true,experiment:'hotel_geo_stop',pid:4242,started_at:t0,fault_active:true,fault:{target:'hotel-sandbox-geo-1',fault:'container_stop'},log_tail:['  [baseline] ok   status=200    42.1ms','injecting container_stop into hotel-sandbox-geo-1 for up to 20s']});
  await openChaosRuns(page);
  const live=page.getByText('Run in progress · hotel_geo_stop');
  await expect(live).toBeVisible();await expect(page.getByText('FAULT ACTIVE',{exact:true})).toBeVisible();await expect(page.getByRole('button',{name:'Stop run'})).toBeEnabled();
});

test('suggested experiments are shown as proposals only, with policy refusals listed',async({page})=>{
  await mockChaos(page,idle);await openChaosRuns(page);
  const panel=page.getByLabel('Suggested experiments');
  await expect(panel).toBeVisible();await expect(panel).toContainText('hotel-rate-stop');await expect(panel).toContainText('PROPOSALS ONLY');
  await expect(panel.getByRole('button',{name:'Copy JSON'})).toBeVisible();
  await panel.getByText('1 candidates refused by policy').click();await expect(panel).toContainText('not allowlisted');
  await expect(page.getByRole('button',{name:'Run',exact:true})).toHaveCount(1); // only the real experiment has Run; a proposal has none
});

test('findings are ranked with their runs, fix and a report download',async({page})=>{
  await mockChaos(page,idle);await openChaosRuns(page);
  const panel=page.getByLabel('Findings');
  await expect(panel).toContainText('NOT RECOVERED / CRASHED');await expect(panel).toContainText('Run at least two replicas');
  await panel.getByRole('button',{name:run.run_id}).click();await expect(page.getByLabel('Run detail')).toBeVisible();
  const download=page.waitForEvent('download');await panel.getByRole('button',{name:'Download Markdown'}).click();
  expect((await download).suggestedFilename()).toBe('chaos-findings.md');
});
