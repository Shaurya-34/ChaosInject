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
const findings={findings:[{id:'hotel-sandbox-geo-1:container_stop',severity:1,severity_label:'Not recovered / crashed',title:'container_stop on hotel-sandbox-geo-1: not recovered / crashed',target:'hotel-sandbox-geo-1',fault:'container_stop',affected_endpoints:['/hotels'],explanation:'In 1 run(s), the system did not recover after the fault was removed.',suggested_fix:'Run at least two replicas behind service discovery.',run_ids:[run.run_id,'20261017-000002-hotel_geo_stop-def456'],explanation_source:'deterministic',evidence:[]}],runs_analyzed:1,runs_without_findings:0,runs_not_evaluated:0,limitations:[]};
const series=(f:(i:number)=>number)=>Array.from({length:12},(_,i)=>[t0-5+i,f(i)]);
const availableMetrics={window:{start:t0-5,end:t0+6},step_s:1,available:true,reason:null,series:{
  controller_latency_ms:series(i=>(i>=8?5:40+i)),fault_active:series(i=>(i>=8&&i<11?1:0)),probe_success:series(i=>(i>=8?0:1)),
  probe_duration_ms:series(i=>14+i),target_net_tx_pps:series(i=>50-i),target_memory_bytes:series(()=>52428800),target_last_seen_age_s:series(i=>(i>=8?i:0))}};
const defaultMetrics={status:200,body:{window:{start:t0,end:t0},step_s:1,available:false,reason:'no data in this window (the observability stack was not running)',series:{}}};
const pair=(fault:string,score:number,extra:object={})=>({fault,score,uncapped_score:score,capped:false,cap_reason:null,not_recovered:false,scored_run_id:run.run_id,run_ids:[run.run_id],
  inputs:{recovered:true,error_rate:1,baseline_p95_ms:42,during_p95_ms:null,recovery_s:1.2,recovery_timeout_s:30,stop_reason:'safety: 5 consecutive failures'},...extra});
const partialScore={formula_version:1,score:65.2,headline:'65.2 (partial: 2 of 9 pairs tested)',complete:false,weights:{recovered:0.4,error:0.25,latency:0.2,recovery_time:0.15},safety_stop_cap:50,
  coverage:{tested:2,possible:9,fraction:0.2222,untested:[{target:'hotel-sandbox-rate-1',fault:'packet_loss'}]},
  targets:[{target:'hotel-sandbox-geo-1',node:'geo',score:50,criticality:0.45,weight:0.45,pairs:[pair('container_stop',50,{uncapped_score:55,capped:true,cap_reason:'safety stop'})]},
           {target:'hotel-sandbox-memcached-rate-1',node:'memcached-rate',score:0,criticality:0.45,weight:0.45,pairs:[pair('container_stop',0,{not_recovered:true})]}],
  unmapped_targets:[],runs_not_scored:[]};
const nullScore={formula_version:1,score:null,headline:'n/a (no scored runs yet)',complete:false,weights:{},safety_stop_cap:50,
  coverage:{tested:0,possible:9,fraction:0,untested:[]},targets:[],unmapped_targets:[],runs_not_scored:[]};
const readyEnv={execution_enabled:true,sandbox:{url:'http://127.0.0.1:5000/hotels',reachable:true,status:200},prometheus:{reachable:true,status:200},runs_saved:1,checked_at:t0};
const idle={running:false,experiment:null,pid:null,started_at:null,exit_code:null,run_id:null,fault_active:false,fault:null,stop_requested:false,log_tail:[]};

async function mockChaos(page:Page,active:object,onRun?:()=>{status:number,body:object},metrics:{status:number,body:object}=defaultMetrics,scoreBody:object=partialScore,findingsBody:object=findings,proposalsBody:object=proposals,envBody:object=readyEnv,runsBody:object[]=index){
  await page.route('**/api/v1/chaos/**',route=>{
    const url=new URL(route.request().url());const path=url.pathname.replace('/api/v1/chaos','');
    const json=(body:object,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    if(path==='/experiments')return json([{id:'hotel_geo_stop',name:'hotel_geo_stop',target:'hotel-sandbox-geo-1',fault:'container_stop',duration_s:20}]);
    if(path==='/environment')return json(envBody);
    if(path==='/runs')return json(runsBody);
    if(path==='/runs/'+run.run_id+'/metrics')return json(metrics.body,metrics.status);
    if(path==='/runs/'+run.run_id)return json(run);
    if(path==='/score')return json(scoreBody);
    if(path==='/findings')return json(findingsBody);
    if(path.startsWith('/findings/report'))return route.fulfill({status:200,contentType:'text/markdown',body:'# Chaos findings'});
    if(path==='/proposals')return json(proposalsBody);
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

test('Prometheus charts appear under the run detail with the controller chart still primary',async({page})=>{
  await mockChaos(page,idle,undefined,{status:200,body:availableMetrics});await openChaosRuns(page);
  await page.getByRole('row',{name:/hotel_geo_stop/}).click();
  const detail=page.getByLabel('Run detail');
  await expect(detail.getByRole('img',{name:/Latency over time/})).toBeVisible();
  const prom=page.getByLabel('Prometheus metrics');
  await expect(prom).toBeVisible();
  for(const title of [/Independent probe vs controller probe/,/Independent probe success/,/Target container network/,/Target container memory/,/Target last seen/])
    await expect(prom.getByLabel(title)).toBeVisible();
  await expect(prom.getByTestId('prom-note')).toHaveCount(0);
});

test('a run without Prometheus data shows a quiet note, not an error',async({page})=>{
  await mockChaos(page,idle);await openChaosRuns(page);
  await page.getByRole('row',{name:/hotel_geo_stop/}).click();
  const prom=page.getByLabel('Prometheus metrics');
  await expect(prom.getByTestId('prom-note')).toContainText('no data in this window');
  await expect(prom.getByRole('alert')).toHaveCount(0);
  await expect(page.getByRole('status').filter({hasText:'Prometheus'})).toHaveCount(0);
  await expect(page.getByLabel('Run detail').getByRole('img',{name:/Latency over time/})).toBeVisible();
});

test('when Prometheus is unreachable the run detail still works',async({page})=>{
  await mockChaos(page,idle,undefined,{status:200,body:{window:{start:t0,end:t0},step_s:1,available:false,reason:'prometheus unreachable',series:{}}});await openChaosRuns(page);
  await page.getByRole('row',{name:/hotel_geo_stop/}).click();
  await expect(page.getByTestId('prom-note')).toContainText('prometheus unreachable');
  await expect(page.getByLabel('Run detail')).toContainText('safety: error rate 100% over 3 probes');
});

test('a failing metrics request is also only a quiet note',async({page})=>{
  await mockChaos(page,idle,undefined,{status:500,body:{code:'DEPENDENCY_UNAVAILABLE',message:'x'}});await openChaosRuns(page);
  await page.getByRole('row',{name:/hotel_geo_stop/}).click();
  await expect(page.getByTestId('prom-note')).toContainText('could not be loaded');
  await expect(page.getByLabel('Run detail').getByRole('img',{name:/Latency over time/})).toBeVisible();
});

test('Run, Stop and Recover are greyed out when the API reports execution disabled',async({page})=>{
  const fault={target:'hotel-sandbox-geo-1',fault:'container_stop'};
  await mockChaos(page,{...idle,execution_enabled:false,fault_active:true,fault});await openChaosRuns(page);
  await expect(page.getByRole('button',{name:'Run',exact:true})).toBeDisabled();
  await expect(page.getByRole('alert').filter({hasText:'A fault is still in place'}).getByRole('button',{name:'Recover'})).toBeDisabled();
  await expect(page.getByText('Run, Stop and Recover stay grey')).toBeVisible();
});

test('the Stop button is greyed out while a run is shown with execution disabled',async({page})=>{
  await mockChaos(page,{...idle,execution_enabled:false,running:true,experiment:'hotel_geo_stop',pid:1,log_tail:['  [baseline] ok   status=200    42.1ms']});await openChaosRuns(page);
  await expect(page.getByRole('button',{name:'Stop run'})).toBeDisabled();
});

test('run ids in a finding are separated',async({page})=>{
  await mockChaos(page,idle);await openChaosRuns(page);
  await expect(page.getByLabel('Findings').getByText(/Runs: .*hotel_geo_stop-abc123, 20261017-000002/)).toBeVisible();
});

test('the header uses ChaosInject branding',async({page})=>{
  await mockChaos(page,idle);await openChaosRuns(page);
  await expect(page.getByText('CHAOSINJECT')).toBeVisible();
  expect(await page.locator('body').innerText()).not.toMatch(/bosch|kind-bosch|boutique/i);
  await expect(page).toHaveTitle('ChaosInject');
});

// Colours: no red, orange, yellow or pink anywhere on the rendered page (HTML and SVG), tints included.
async function warmColours(page:Page):Promise<string[]>{
  return page.evaluate(()=>{
    const warm=(c:string)=>{
      const m=c.match(/rgba?\(([^)]+)\)/);if(!m)return false;
      const [r,g,b,a=1]=m[1].split(/[ ,\/]+/).filter(Boolean).map(Number);
      if(a<0.05)return false;
      const R=r/255,G=g/255,B=b/255,max=Math.max(R,G,B),min=Math.min(R,G,B),l=(max+min)/2,d=max-min;
      if(d===0)return false;
      const s=l>0.5?d/(2-max-min):d/(max+min);
      let h=max===R?((G-B)/d)%6:max===G?(B-R)/d+2:(R-G)/d+4;h=(h*60+360)%360;
      return s>0.12&&(h<45||h>=320);
    };
    const props=['color','backgroundColor','borderTopColor','borderRightColor','borderBottomColor','borderLeftColor','outlineColor','fill','stroke'];
    const bad=new Set<string>();
    for(const el of Array.from(document.querySelectorAll('body *'))){
      const cs=getComputedStyle(el);
      for(const p of props){const v=(cs as unknown as Record<string,string>)[p];if(v&&warm(v))bad.add(`${el.tagName.toLowerCase()}.${(el.getAttribute('class')||'').split(' ')[0]} ${p}=${v}`);}
    }
    return [...bad];
  });
}

test('a run with a leftover fault, findings, proposals and charts uses cold colours only',async({page})=>{
  await mockChaos(page,{...idle,fault_active:true,fault:{target:'hotel-sandbox-geo-1',fault:'container_stop'}},undefined,{status:200,body:availableMetrics});await openChaosRuns(page);
  await page.getByRole('row',{name:/hotel_geo_stop/}).click();
  await expect(page.getByLabel('Prometheus metrics').getByLabel(/Target last seen/)).toBeVisible();
  await expect(page.getByLabel('Findings')).toBeVisible();await expect(page.getByLabel('Suggested experiments')).toBeVisible();
  expect(await warmColours(page)).toEqual([]);
});

test('the live run panel uses cold colours only',async({page})=>{
  await mockChaos(page,{...idle,running:true,experiment:'hotel_geo_stop',pid:1,fault_active:true,fault:{target:'hotel-sandbox-geo-1',fault:'container_stop'},log_tail:['  [baseline] ok   status=200    42.1ms']});await openChaosRuns(page);
  await expect(page.getByRole('button',{name:'Stop run'})).toBeVisible();
  expect(await warmColours(page)).toEqual([]);
});

test('Emergency STOP is a filled violet button and the strongest control',async({page})=>{
  await mockChaos(page,idle);await openChaosRuns(page);
  const stop=page.getByRole('button',{name:'Emergency STOP'});
  await expect(stop).toBeVisible();
  const style=await stop.evaluate(el=>{const c=getComputedStyle(el);return {bg:c.backgroundColor,color:c.color,weight:c.fontWeight};});
  expect(style).toEqual({bg:'rgb(95, 61, 196)',color:'rgb(255, 255, 255)',weight:'800'});
});

test('the colour scanner itself detects warm colours and passes cold ones',async({page})=>{
  await page.setContent('<div style="background:#b14343">x</div><div style="color:#c57820">y</div><div style="background:#fff8f8">z</div><svg><rect fill="#f6d9d9" width="5" height="5"/></svg>');
  expect((await warmColours(page)).length).toBeGreaterThanOrEqual(4);
  await page.setContent('<div style="background:#5f3dc4;color:#fff;border:1px solid #d0bfff">x</div><div style="background:#0b8178">y</div><svg><rect fill="#d0bfff" stroke="#1864ab" width="5" height="5"/></svg>');
  expect(await warmColours(page)).toEqual([]);
});

test('the header chip shows the headline score and opens the Chaos runs page',async({page})=>{
  await mockChaos(page,idle);
  await page.goto('/');await page.getByLabel('Operator token').fill(token);await page.getByRole('button',{name:'Connect workspace'}).click();
  const chip=page.getByRole('button',{name:/Resilience score: 65.2/});
  await expect(chip).toBeVisible();await expect(chip).toContainText('65.2');await expect(chip).toContainText('partial');
  await chip.click();
  await expect(page.getByLabel('Resilience score',{exact:true})).toBeVisible();
});

test('the score card shows the number, coverage, the cap and the traceable runs',async({page})=>{
  await mockChaos(page,idle);await openChaosRuns(page);
  const card=page.getByLabel('Resilience score',{exact:true});
  await expect(card.getByLabel('Resilience score 65.2')).toBeVisible();
  await expect(card).toContainText('65.2 (partial: 2 of 9 pairs tested)');
  await expect(card.getByLabel('Coverage')).toContainText('2 of 9');
  await card.getByText('Breakdown by target and fault').click();
  await expect(card).toContainText('capped: safety stop (from 55)');
  await expect(card).toContainText('not recovered → 0');
  await card.getByRole('button',{name:run.run_id}).first().click();
  await expect(page.getByLabel('Run detail')).toBeVisible();
});

test('with no scored runs the score is n/a and says what to do next',async({page})=>{
  await mockChaos(page,idle,undefined,undefined,nullScore);
  await page.goto('/');await page.getByLabel('Operator token').fill(token);await page.getByRole('button',{name:'Connect workspace'}).click();
  await expect(page.getByRole('button',{name:/Resilience score: n\/a/})).toContainText('n/a');
  await page.getByRole('button',{name:'Chaos runs',exact:true}).click();
  const card=page.getByLabel('Resilience score',{exact:true});
  await expect(card.getByLabel('Resilience score n/a')).toBeVisible();
  await expect(card).toContainText('No scored runs yet');
  await expect(card).not.toContainText(/0\/100/);
});

const liveFindings=(location:string)=>({...findings,llm:{mode:'live',model:'llama-3.3-70b-versatile',location},
  findings:[{...findings.findings[0],explanation:'Model-written wording for this finding.',explanation_source:'live',explanation_model:'llama-3.3-70b-versatile'}]});
const liveProposals=(location:string)=>({...proposals,llm:{mode:'live',model:'llama-3.3-70b-versatile',location},
  proposals:[{...proposals.proposals[0],rationale:'Model-written rationale.',rationale_source:'live',rationale_model:'llama-3.3-70b-versatile'}]});

test('without a language model both panels say the text is rule-based',async({page})=>{
  await mockChaos(page,idle);await openChaosRuns(page);
  await expect(page.getByLabel('Findings').getByText('Rule-based explanation')).toBeVisible();
  await expect(page.getByLabel('Suggested experiments').getByText('Rule-based explanation')).toBeVisible();
  await expect(page.getByText(/Explained by/)).toHaveCount(0);
});

test('model-written text is labelled with the model and where it runs',async({page})=>{
  await mockChaos(page,idle,undefined,undefined,undefined,liveFindings('hosted'),liveProposals('local'));await openChaosRuns(page);
  const f=page.getByLabel('Findings');
  await expect(f).toContainText('Model-written wording for this finding.');
  await expect(f.getByText('Explained by llama-3.3-70b-versatile (hosted)')).toBeVisible();
  const s=page.getByLabel('Suggested experiments');
  await expect(s).toContainText('Model-written rationale.');
  await expect(s.getByText('Explained by llama-3.3-70b-versatile (local)')).toBeVisible();
  await expect(page.getByText('Rule-based explanation')).toHaveCount(0);
});

test('when the model could not help, the panel states why and shows the rule-based text',async({page})=>{
  const note='The language model gave no usable text for 1 of 1 item(s) (unreachable, too slow, an unexpected answer, or a number that was not in the evidence); rule-based text is shown for them.';
  await mockChaos(page,idle,undefined,undefined,undefined,{...findings,llm:{mode:'live',model:'llama-3.3-70b-versatile',location:'hosted'},limitations:[note]});await openChaosRuns(page);
  const f=page.getByLabel('Findings');
  await expect(f).toContainText('gave no usable text');
  await expect(f.getByText('Rule-based explanation')).toBeVisible();
  expect(await warmColours(page)).toEqual([]);
});

// ---- findings show the latest run's state ----------------------------------------------------------------

const statusFindings=(over:Record<string,unknown>)=>({...findings,findings:[{...findings.findings[0],runs_total:3,runs_with_weakness:2,latest_run_id:run.run_id,...over}]});

test('a finding that was fixed by a later run says so instead of reading as still broken',async({page})=>{
  const body=statusFindings({status:'fixed_since',status_label:'Fixed since',latest_state:'No weakness',latest_severity:null,
    explanation:'Fixed since: the latest run showed no weakness, but 2 of the 2 earlier run(s) did; at its worst, the system did not recover after the fault was removed.'});
  await mockChaos(page,idle,undefined,undefined,undefined,body);await openChaosRuns(page);
  const card=page.getByLabel('Findings').locator('article.finding-card');
  await expect(card).toHaveClass(/status-fixed_since/);
  await expect(card.locator('[data-status="fixed_since"]')).toHaveText('FIXED SINCE');
  await expect(card).toContainText('Fixed since: the latest run showed no weakness');
  await expect(card.locator('.latest-state')).toContainText('Latest run: No weakness · 2 of 3 runs showed a weakness');
  expect(await warmColours(page)).toEqual([]);
});

test('a milder latest run is marked improved since',async({page})=>{
  const body=statusFindings({status:'improved',status_label:'Improved since',latest_state:'Degraded but tolerated',latest_severity:4,
    explanation:'Improved since: the latest run is milder (the system degraded during the fault but recovered); at its worst, the system did not recover after the fault was removed. 3 of 3 runs showed a weakness.'});
  await mockChaos(page,idle,undefined,undefined,undefined,body);await openChaosRuns(page);
  const card=page.getByLabel('Findings').locator('article.finding-card');
  await expect(card.locator('[data-status="improved"]')).toHaveText('IMPROVED SINCE');
  await expect(card.locator('.latest-state')).toContainText('Latest run: Degraded but tolerated');
});

test('an open finding has no status tag and keeps its wording',async({page})=>{
  const body=statusFindings({status:'open',status_label:'Open',latest_state:'Not recovered / crashed',latest_severity:1,runs_total:1,runs_with_weakness:1,
    explanation:'In 1 run(s), the system did not recover after the fault was removed.'});
  await mockChaos(page,idle,undefined,undefined,undefined,body);await openChaosRuns(page);
  const card=page.getByLabel('Findings').locator('article.finding-card');
  await expect(card).toContainText('In 1 run(s), the system did not recover');
  await expect(card.locator('[data-status]')).toHaveCount(0);
  await expect(card.locator('.latest-state')).toHaveCount(0);
});

// ---- G3: empty and first-run states --------------------------------------------------------------------------

const env=(over:Record<string,unknown>={})=>({execution_enabled:true,sandbox:{url:'http://127.0.0.1:5000/hotels',reachable:true,status:200},
  prometheus:{reachable:true,status:200},runs_saved:3,checked_at:t0,...over});
const noFindings={findings:[],runs_analyzed:0,runs_without_findings:0,runs_not_evaluated:0,limitations:[],llm:{mode:'deterministic',model:null,location:null}};
const checklist=(page:Page)=>page.getByLabel('Getting started');

test('a fresh clone with no runs says what to do next in every panel',async({page})=>{
  await mockChaos(page,idle,undefined,undefined,nullScore,noFindings,undefined,env({runs_saved:0}),[]);await openChaosRuns(page);
  await expect(checklist(page).locator('[data-item="no-runs"]')).toContainText('Start one from Experiments below');
  await expect(page.getByLabel('Runs').or(page.locator('section.panel',{hasText:'Runs'})).first()).toBeVisible();
  await expect(page.getByText('No runs yet. Start one from Experiments.')).toBeVisible();
  await expect(page.getByLabel('Resilience score',{exact:true})).toContainText('No scored runs yet');
  await expect(page.getByLabel('Findings')).toContainText('No runs yet, so there are no findings');
  await expect(page.getByLabel('Findings')).not.toContainText('No weaknesses found');
  await expect(page.getByLabel('Run detail')).toHaveCount(0);
  await expect(page.getByRole('alert')).toHaveCount(0);
});

test('when everything is ready no getting-started panel is shown',async({page})=>{
  await mockChaos(page,idle);await openChaosRuns(page);
  await expect(page.getByText('Select a run above to see its chart and the stop reason.')).toBeVisible();
  await expect(checklist(page)).toHaveCount(0);
});

test('execution disabled is explained up front with the command to enable it',async({page})=>{
  await mockChaos(page,{...idle,execution_enabled:false},undefined,undefined,undefined,undefined,undefined,env({execution_enabled:false}));await openChaosRuns(page);
  const item=checklist(page).locator('[data-item="execution"]');
  await expect(item).toContainText('Execution is disabled');
  await expect(item.locator('code')).toContainText('python scripts/dev.py up --execute');
  await expect(checklist(page)).toContainText('ACTION NEEDED');
  await expect(page.getByRole('button',{name:'Run',exact:true})).toBeDisabled();
  await expect(page.getByRole('alert')).toHaveCount(0);
});

test('a stopped sandbox is explained with how to start it',async({page})=>{
  await mockChaos(page,idle,undefined,undefined,undefined,undefined,undefined,env({sandbox:{url:'http://127.0.0.1:5000/hotels',reachable:false,status:null}}));await openChaosRuns(page);
  const item=checklist(page).locator('[data-item="sandbox"]');
  await expect(item).toContainText('The hotel sandbox is not running');
  await expect(item).toContainText('http://127.0.0.1:5000/hotels');
  await expect(item.locator('code')).toContainText('python scripts/dev.py up');
});

test('an unhealthy sandbox is named with its HTTP status',async({page})=>{
  await mockChaos(page,idle,undefined,undefined,undefined,undefined,undefined,env({sandbox:{url:'http://127.0.0.1:5000/hotels',reachable:true,status:503}}));await openChaosRuns(page);
  const item=checklist(page).locator('[data-item="sandbox-unhealthy"]');
  await expect(item).toContainText('HTTP 503');
  await expect(item.locator('code')).toContainText('python scripts/dev.py status');
  await expect(checklist(page).locator('[data-item="sandbox"]')).toHaveCount(0);
});

test('Prometheus being down is a quiet hint, and run detail still works',async({page})=>{
  await mockChaos(page,idle,undefined,{status:200,body:{window:{start:t0,end:t0},step_s:1,available:false,reason:'prometheus unreachable',series:{}}},
    undefined,undefined,undefined,env({prometheus:{reachable:false,status:null}}));await openChaosRuns(page);
  const item=checklist(page).locator('[data-item="prometheus"]');
  await expect(item).toContainText('Prometheus is not running');
  await expect(item).toContainText('Experiments still work');
  await expect(checklist(page)).toContainText('FOR INFORMATION');
  await page.getByRole('row',{name:/hotel_geo_stop/}).click();
  await expect(page.getByTestId('prom-note')).toContainText('prometheus unreachable');
  await expect(page.getByLabel('Run detail').getByRole('img',{name:/Latency over time/})).toBeVisible();
  await expect(page.getByRole('alert')).toHaveCount(0);
});

// CI runs Linux, whose default fonts are wider than Windows or macOS (that is how the header overflow got through
// locally). Forcing wide fonts makes the phone-width checks mean the same thing on every machine.
async function useWideFonts(page:Page){
  await page.addStyleTag({content:"*{font-family:Verdana,Georgia,sans-serif !important} code,pre,.firstrun-command,.catalog-id,.score-chip,.chaos-log{font-family:'Courier New','Lucida Console',monospace !important}"});
  await page.waitForTimeout(300);
}

// Name the elements that stick out, so a failure says what to fix rather than only that something is too wide.
async function sticksOut(page:Page):Promise<string[]>{
  return page.evaluate(()=>{
    const out=[...document.body.querySelectorAll('*')].filter(e=>{
      const r=e.getBoundingClientRect();return r.width>0&&r.right>window.innerWidth+0.5&&!e.closest('table')&&!e.closest('svg')&&!e.closest('details:not([open])');
    }).slice(0,12).map(e=>`${e.tagName.toLowerCase()}.${String(e.className).slice(0,40)} right=${Math.round(e.getBoundingClientRect().right)} of ${window.innerWidth}: "${(e.textContent??'').trim().slice(0,30)}"`);
    return document.documentElement.scrollWidth>window.innerWidth?[`page scrollWidth ${document.documentElement.scrollWidth} > ${window.innerWidth}`,...out]:out;
  });
}

test('every problem at once is listed, uses cold colours and fits a phone screen',async({page})=>{
  await mockChaos(page,{...idle,execution_enabled:false},undefined,undefined,nullScore,noFindings,undefined,
    env({execution_enabled:false,runs_saved:0,sandbox:{url:'http://127.0.0.1:5000/hotels',reachable:false,status:null},prometheus:{reachable:false,status:null}}),[]);
  await page.setViewportSize({width:390,height:844});await openChaosRuns(page);
  for(const id of ['execution','sandbox','prometheus','no-runs'])await expect(checklist(page).locator(`[data-item="${id}"]`)).toBeVisible();
  expect(await warmColours(page)).toEqual([]);
  await useWideFonts(page);
  expect(await sticksOut(page)).toEqual([]);
});

test('with data on screen (partial score chip, a selected run, charts) the whole page fits a phone screen',async({page})=>{
  await mockChaos(page,idle,undefined,{status:200,body:availableMetrics});
  await page.setViewportSize({width:390,height:844});await openChaosRuns(page);
  await page.getByRole('row',{name:/hotel_geo_stop/}).click();
  await expect(page.getByLabel('Run detail')).toBeVisible();
  await expect(page.getByLabel('Prometheus metrics').getByLabel(/Target last seen/)).toBeVisible();
  await expect(page.getByRole('button',{name:/Resilience score: 65.2/})).toBeVisible();
  await useWideFonts(page);
  expect(await sticksOut(page)).toEqual([]);
});

test('the environment status refreshes, so starting the sandbox clears its item',async({page})=>{
  let reachable=false;
  await mockChaos(page,idle);
  await page.route('**/api/v1/chaos/environment',route=>route.fulfill({status:200,contentType:'application/json',
    body:JSON.stringify(env({sandbox:{url:'http://127.0.0.1:5000/hotels',reachable,status:reachable?200:null}}))}));
  await openChaosRuns(page);
  await expect(checklist(page).locator('[data-item="sandbox"]')).toBeVisible();
  reachable=true;
  await expect(checklist(page)).toHaveCount(0,{timeout:15000});
});
