import React,{useCallback,useEffect,useRef,useState} from 'react';
import {AlertTriangle,OctagonX,Play,RotateCcw} from 'lucide-react';
import {CartesianGrid,ComposedChart,Legend,Line,ReferenceArea,ResponsiveContainer,Scatter,Tooltip,XAxis,YAxis} from 'recharts';
import {PrometheusCharts} from './PrometheusCharts';
import {ScoreCard} from './Score';
import {FirstRunChecklist} from './FirstRun';
import {ChaosApiError,chaos,chaosDownload,explainedBy,phaseFromLog,refusalMessage,type ActiveRun,type ChaosRun,type ExperimentRow,type IndexEntry,type Proposals,type Findings,type Score,type Environment} from './chaosApi';

const pct=(x:number|null|undefined)=>x==null?'n/a':`${Math.round(x*100)}%`;
const ms=(x:number|null|undefined)=>x==null?'n/a':`${Math.round(x)} ms`;
const secs=(x:number|null|undefined)=>x==null?'n/a':`${x.toFixed(1)} s`;

export function ChaosRuns({source}:{source?:string}){
  const [experiments,setExperiments]=useState<ExperimentRow[]>([]);
  const [runs,setRuns]=useState<IndexEntry[]>([]);
  const [suggested,setSuggested]=useState<Proposals>();
  const [findings,setFindings]=useState<Findings>();
  const [score,setScore]=useState<Score>();
  const [env,setEnv]=useState<Environment>();
  const [active,setActive]=useState<ActiveRun>();
  const [detail,setDetail]=useState<ChaosRun>();
  const [selected,setSelected]=useState<string>();
  const [message,setMessage]=useState('');
  const [execDisabled,setExecDisabled]=useState(false);
  const [busy,setBusy]=useState(false);
  const wasRunning=useRef(false);

  const loadRuns=useCallback(async()=>{setRuns(await chaos<IndexEntry[]>('/runs'));},[]);
  const openRun=useCallback(async(id:string)=>{
    setSelected(id);
    try{setDetail(await chaos<ChaosRun>('/runs/'+encodeURIComponent(id)));}catch(e){setMessage(refusalMessage(e));}
  },[]);

  useEffect(()=>{
    chaos<ExperimentRow[]>('/experiments').then(setExperiments).catch(e=>setMessage(refusalMessage(e)));
    chaos<Proposals>('/proposals').then(setSuggested).catch(()=>{});
    chaos<Findings>('/findings').then(setFindings).catch(()=>{});
    chaos<Score>('/score').then(setScore).catch(()=>{});
    loadRuns().catch(e=>setMessage(refusalMessage(e)));
  },[loadRuns]);

  // What a first run needs (execution, sandbox, Prometheus, saved runs): on load, then every 10 s.
  useEffect(()=>{
    let stopped=false;
    const load=()=>chaos<Environment>('/environment').then(e=>{if(!stopped)setEnv(e);}).catch(()=>{});
    load();
    const timer=setInterval(load,10000);
    return()=>{stopped=true;clearInterval(timer);};
  },[]);

  // Poll /active: once a second while a run is in flight, every 3 s otherwise (to notice a leftover fault).
  useEffect(()=>{
    let stopped=false;
    let timer:ReturnType<typeof setTimeout>|undefined;
    const tick=async()=>{
      let delay=3000;
      try{
        const a=await chaos<ActiveRun>('/active');
        if(stopped)return;
        setActive(a);
        if(a.running)delay=1000;
        if(wasRunning.current&&!a.running){
          loadRuns().catch(()=>{});
          chaos<Findings>('/findings').then(setFindings).catch(()=>{});
          chaos<Score>('/score').then(setScore).catch(()=>{});
          if(a.run_id)openRun(a.run_id);
        }
        wasRunning.current=a.running;
      }catch(e){if(!stopped)setMessage(refusalMessage(e));}
      if(!stopped)timer=setTimeout(tick,delay);
    };
    tick();
    return()=>{stopped=true;if(timer)clearTimeout(timer);};
  },[loadRuns,openRun]);

  async function call(path:string,method:'GET'|'POST',after?:()=>void){
    setBusy(true);setMessage('');
    try{await chaos(path,method);after?.();}
    catch(e){
      if(e instanceof ChaosApiError&&e.code==='MUTATIONS_DISABLED')setExecDisabled(true);
      setMessage(refusalMessage(e));
    }finally{setBusy(false);}
  }
  const refreshActive=()=>{chaos<ActiveRun>('/active').then(setActive).catch(()=>{});};
  const start=(id:string)=>call(`/experiments/${encodeURIComponent(id)}/run`,'POST',()=>{wasRunning.current=true;refreshActive();});
  const running=!!active?.running;
  const execOff=execDisabled||active?.execution_enabled===false;
  const leftover=!!active&&!active.running&&active.fault_active;

  return <div className="chaos">
    <p className="muted">Saved results from <code>results/</code> via <code>/api/v1/chaos</code>. {source==='fixture'?'The API is in fixture mode, so any runs shown here may be synthetic examples. ':''}Starting, stopping and recovering need <code>CHAOS_EXECUTION_ENABLED=true</code> on the API.</p>
    {message&&<div role="status" className="notice">{message}<button aria-label="Dismiss message" onClick={()=>setMessage('')}>×</button></div>}

    <FirstRunChecklist env={env}/>
    <ScoreCard score={score} onOpenRun={openRun}/>
    {leftover&&<section className="chaos-banner" role="alert"><AlertTriangle size={20}/><div><strong>A fault is still in place</strong><p>No controller is running, but a fault on <code>{active!.fault?.target??'a target'}</code> was not restored.</p></div><button className="primary" disabled={busy||execOff} title={execOff?'Execution disabled':'Restore the leftover fault'} onClick={()=>call('/recover','POST',refreshActive)}><RotateCcw size={16}/> Recover</button></section>}

    {running&&<section className="panel chaos-live" aria-live="polite"><div className="panel-title"><h2>Run in progress · {active!.experiment}</h2><span className="small-tag">{phaseFromLog(active!.log_tail).toUpperCase()}</span></div>
      <dl><dt>Target</dt><dd>{active!.fault?.target??'none yet (baseline)'}</dd><dt>Fault active</dt><dd>{active!.fault_active?'yes':'no'}</dd><dt>Controller pid</dt><dd>{active!.pid??'starting'}</dd><dt>Stop requested</dt><dd>{active!.stop_requested?'yes':'no'}</dd></dl>
      <pre className="chaos-log" aria-label="Controller log">{active!.log_tail.slice(-8).join('\n')}</pre>
      <button className="chaos-stop" disabled={busy||active!.stop_requested||execOff} title={execOff?'Execution disabled':'Stop the run and restore the fault'} onClick={()=>call('/active/stop','POST')}><OctagonX size={17}/> Stop run</button>
    </section>}

    <div className="grid">
      <section className="panel"><h2>Experiments</h2>
        {experiments.length===0&&<p className="empty">No experiment files found in <code>experiments/</code>. Add one (see the README) and reload.</p>}
        {experiments.map(x=><div className="catalog" key={x.id}>
          <span className="catalog-id">{x.fault??'?'}</span>
          <div><strong>{x.id}</strong><p>{x.target} · {x.duration_s}s</p></div>
          <button className="primary" disabled={busy||running||execOff} title={execOff?'Execution disabled':running?'A run is active':'Run this experiment'} onClick={()=>start(x.id)}><Play size={15}/> Run</button>
        </div>)}
        {execOff&&<p className="muted">Execution disabled: Run, Stop and Recover stay grey until the API is started with CHAOS_EXECUTION_ENABLED=true.</p>}
      </section>
      <section className="panel"><h2>Runs</h2>
        {runs.length===0?<p className="empty">No runs yet. Start one from Experiments.</p>:<div className="chaos-table-wrap"><table className="chaos-table"><thead><tr><th>Experiment</th><th>Fault</th><th>Target</th><th>Error rate</th><th>p95 (base → fault)</th><th>Recovery</th><th>Stop reason</th></tr></thead><tbody>
          {runs.map(r=><tr key={r.run_id} className={(r.recovered===false?'bad ':'')+(r.run_id===selected?'selected':'')} onClick={()=>openRun(r.run_id)} tabIndex={0} onKeyDown={e=>{if(e.key==='Enter')openRun(r.run_id);}}>
            <td>{r.experiment}</td><td>{r.fault??'n/a'}</td><td>{r.target}</td><td>{pct(r.during_error_rate)}</td><td>{ms(r.baseline_p95_ms)} → {ms(r.during_p95_ms)}</td>
            <td>{r.recovered===false?'NOT RECOVERED':secs(r.recovery_s)}</td><td>{r.stop_reason??'n/a'}</td></tr>)}
        </tbody></table></div>}
      </section>
    </div>

    {findings&&<FindingsPanel data={findings} onOpen={openRun} onError={e=>setMessage(refusalMessage(e))}/>}
    {suggested&&<SuggestedExperiments data={suggested} onCopied={()=>setMessage('Experiment JSON copied. Review it, then save it under experiments/ to make it runnable.')}/>}
    {detail?<RunDetail run={detail}/>:runs.length>0&&<p className="muted">Select a run above to see its chart and the stop reason.</p>}
  </div>;
}

type Point={x:number;latency?:number;failed?:number};

function RunDetail({run}:{run:ChaosRun}){
  const all=[...run.baseline,...run.during,...run.recovery];
  const t0=all.length?Math.min(...all.map(s=>s.t)):0;
  const rel=(t:number)=>Math.round((t-t0)*100)/100;
  const points:Point[]=all.map(s=>({x:rel(s.t),...(s.ok?{latency:s.latency_ms}:{failed:s.latency_ms})}));
  const end=all.length?Math.max(...all.map(s=>s.t)):t0;
  const fs=run.fault_started,fe=run.fault_ended;
  const phases=[
    {name:'Baseline',x1:rel(t0),x2:rel(fs??(run.during[0]?.t??end)),fill:'#f2f1f8',opacity:0.05},
    ...(fs!=null?[{name:'Fault',x1:rel(fs),x2:rel(fe??end),fill:'#7c5cff',opacity:0.22}]:[]),
    ...(fe!=null?[{name:'Recovery',x1:rel(fe),x2:rel(end),fill:'#ffe14a',opacity:0.08}]:[]),
  ];
  const bad=!run.recovered;
  return <section className="panel chaos-detail" aria-label="Run detail">
    <div className="panel-title"><h2>{run.experiment.name} · <code>{run.run_id}</code></h2><span className={'small-tag'+(bad?' bad':'')}>{run.refused?'REFUSED':bad?'NOT RECOVERED':'RECOVERED'}</span></div>
    <p className="muted">{run.experiment.fault} on {run.experiment.target} for up to {run.experiment.duration_s}s · recovery {secs(run.recovery_s)}</p>
    <div className={'chaos-callout'+(run.stop_reason?.startsWith('safety')?' safety':'')} role="note"><strong>Stop reason</strong><span>{run.stop_reason??'none recorded'}</span></div>
    {all.length===0?<p className="empty">No probe samples were recorded (run refused before injecting).</p>:
    <div className="chaos-chart" role="img" aria-label="Latency over time with baseline, fault and recovery phases shaded"><ResponsiveContainer width="100%" height={300}>
      <ComposedChart data={points} margin={{top:10,right:20,bottom:20,left:0}}>
        <CartesianGrid strokeDasharray="3 3" stroke="#2d2c3a"/>
        {phases.map(p=><ReferenceArea key={p.name} x1={p.x1} x2={p.x2} fill={p.fill} fillOpacity={p.opacity} ifOverflow="visible" label={{value:p.name,position:'insideTop',fontSize:11,fill:'#a3a1b8'}}/>)}
        <XAxis type="number" dataKey="x" domain={['dataMin','dataMax']} tickFormatter={v=>`${v}s`} label={{value:'seconds since first probe',position:'insideBottom',offset:-10,fontSize:11}}/>
        <YAxis label={{value:'latency (ms)',angle:-90,position:'insideLeft',fontSize:11}}/>
        <Tooltip formatter={(v:number,n:string)=>[`${v} ms`,n]}/>
        <Legend verticalAlign="top" height={28}/>
        <Line type="monotone" dataKey="latency" name="latency (ok probes)" stroke="#ffe14a" strokeWidth={2} dot={{r:2}} connectNulls isAnimationActive={false}/>
        <Scatter dataKey="failed" name="failed probe" fill="#a58bff" shape="cross" isAnimationActive={false}/>
      </ComposedChart>
    </ResponsiveContainer></div>}
    <dl className="chaos-summary">
      {(['baseline','during','recovery'] as const).map(k=><React.Fragment key={k}><dt>{k}</dt><dd>{run.summary[k].count} probes · {run.summary[k].failures} failed · p95 {ms(run.summary[k].p95_ms)}</dd></React.Fragment>)}
    </dl>
    <PrometheusCharts runId={run.run_id}/>
  </section>;
}

function SuggestedExperiments({data,onCopied}:{data:Proposals;onCopied:()=>void}){
  const copy=(p:Proposals['proposals'][number])=>navigator.clipboard?.writeText(JSON.stringify(p.experiment,null,2)).then(onCopied).catch(()=>{});
  return <section className="panel chaos-suggest" aria-label="Suggested experiments">
    <div className="panel-title"><h2>Suggested next experiments</h2><span className="small-tag">PROPOSALS ONLY</span></div>
    <p className="muted">Ranked by the injection agent from the dependency map and run history. Every proposal passed the controller policy check. Nothing here is saved or started; a person decides.</p>
    {data.limitations.map(l=><p key={l} className="muted">{l}</p>)}
    {data.proposals.length===0&&<p className="empty">No allowed experiment to propose.</p>}
    {data.proposals.map((p,i)=><div className="suggest-row" key={p.experiment.name}>
      <span className="rank">{i+1}</span>
      <div><strong>{p.experiment.name}</strong> <span className="small-tag">{p.experiment.fault}</span> <span className="small-tag">score {p.score}</span>
        <p>{p.rationale}</p><p className="muted explain-source" data-source={p.rationale_source}>{explainedBy(p.rationale_source,p.rationale_model,data.llm?.location)}</p><p className="muted">{p.expected_impact}</p></div>
      <button onClick={()=>copy(p)} title="Copy the experiment JSON to the clipboard">Copy JSON</button>
    </div>)}
    {data.rejected.length>0&&<details><summary>{data.rejected.length} candidates refused by policy</summary><ul className="muted">{data.rejected.slice(0,40).map(r=><li key={r.target+r.fault}>{r.target} · {r.fault}: {r.reason}</li>)}</ul></details>}
  </section>;
}

function FindingsPanel({data,onOpen,onError}:{data:Findings;onOpen:(id:string)=>void;onError:(e:unknown)=>void}){
  const save=(format:'md'|'html')=>chaosDownload(`/findings/report?format=${format}`,`chaos-findings.${format}`).catch(onError);
  return <section className="panel chaos-findings" aria-label="Findings">
    <div className="panel-title"><h2>Findings</h2><span><button onClick={()=>save('md')}>Download Markdown</button> <button onClick={()=>save('html')}>Download HTML</button></span></div>
    <p className="muted">Ranked by severity from {data.runs_analyzed} saved run(s). Every finding cites its runs; numbers come from the run records. {data.runs_without_findings} run(s) showed no weakness, {data.runs_not_evaluated} not evaluated.</p>
    {data.limitations.map(l=><p key={l} className="muted">{l}</p>)}
    {data.findings.length===0&&<p className="empty">{data.runs_analyzed===0?'No runs yet, so there are no findings. They appear after your first run.':'No weaknesses found in the saved runs.'}</p>}
    {data.findings.map(f=><article className={'finding-card sev'+f.severity+(f.status&&f.status!=='open'?' status-'+f.status:'')} key={f.id}>
      <div><span className={'small-tag sev'+f.severity}>{f.severity_label.toUpperCase()}</span> {f.status&&f.status!=='open'&&<span className={'small-tag status-'+f.status} data-status={f.status}>{(f.status_label??f.status).toUpperCase()}</span>} <strong>{f.title}</strong></div>
      <p>{f.explanation}</p>
      {f.runs_total!==undefined&&f.runs_total>1&&<p className="muted latest-state">Latest run: {f.latest_state??'n/a'} · {f.runs_with_weakness} of {f.runs_total} runs showed a weakness</p>}
      <p className="muted explain-source" data-source={f.explanation_source}>{explainedBy(f.explanation_source,f.explanation_model,data.llm?.location)}</p>
      <p className="muted">Affects: {f.affected_endpoints.join(', ')||'no working endpoint'} · Runs: {f.run_ids.map((id,i)=><React.Fragment key={id}>{i>0&&', '}<button className="link" onClick={()=>onOpen(id)}>{id}</button></React.Fragment>)}</p>
      <p><strong>Suggested fix.</strong> {f.suggested_fix}</p>
    </article>)}
  </section>;
}
