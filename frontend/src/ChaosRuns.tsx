import React,{useCallback,useEffect,useRef,useState} from 'react';
import {AlertTriangle,OctagonX,Play,RotateCcw} from 'lucide-react';
import {CartesianGrid,ComposedChart,Legend,Line,ReferenceArea,ResponsiveContainer,Scatter,Tooltip,XAxis,YAxis} from 'recharts';
import {ChaosApiError,chaos,phaseFromLog,refusalMessage,type ActiveRun,type ChaosRun,type ExperimentRow,type IndexEntry} from './chaosApi';

const pct=(x:number|null|undefined)=>x==null?'n/a':`${Math.round(x*100)}%`;
const ms=(x:number|null|undefined)=>x==null?'n/a':`${Math.round(x)} ms`;
const secs=(x:number|null|undefined)=>x==null?'n/a':`${x.toFixed(1)} s`;

export function ChaosRuns({source}:{source?:string}){
  const [experiments,setExperiments]=useState<ExperimentRow[]>([]);
  const [runs,setRuns]=useState<IndexEntry[]>([]);
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
    loadRuns().catch(e=>setMessage(refusalMessage(e)));
  },[loadRuns]);

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
  const leftover=!!active&&!active.running&&active.fault_active;

  return <div className="chaos">
    <p className="muted">Saved results from <code>results/</code> via <code>/api/v1/chaos</code>. {source==='fixture'?'The API is in fixture mode, so any runs shown here may be synthetic examples. ':''}Starting, stopping and recovering need <code>CHAOS_EXECUTION_ENABLED=true</code> on the API.</p>
    {message&&<div role="status" className="notice">{message}<button aria-label="Dismiss message" onClick={()=>setMessage('')}>×</button></div>}

    {leftover&&<section className="chaos-banner" role="alert"><AlertTriangle size={20}/><div><strong>A fault is still in place</strong><p>No controller is running, but a fault on <code>{active!.fault?.target??'a target'}</code> was not restored.</p></div><button className="primary" disabled={busy||execDisabled} onClick={()=>call('/recover','POST',refreshActive)}><RotateCcw size={16}/> Recover</button></section>}

    {running&&<section className="panel chaos-live" aria-live="polite"><div className="panel-title"><h2>Run in progress · {active!.experiment}</h2><span className="small-tag">{phaseFromLog(active!.log_tail).toUpperCase()}</span></div>
      <dl><dt>Target</dt><dd>{active!.fault?.target??'none yet (baseline)'}</dd><dt>Fault active</dt><dd>{active!.fault_active?'yes':'no'}</dd><dt>Controller pid</dt><dd>{active!.pid??'starting'}</dd><dt>Stop requested</dt><dd>{active!.stop_requested?'yes':'no'}</dd></dl>
      <pre className="chaos-log" aria-label="Controller log">{active!.log_tail.slice(-8).join('\n')}</pre>
      <button className="chaos-stop" disabled={busy||active!.stop_requested} onClick={()=>call('/active/stop','POST')}><OctagonX size={17}/> Stop run</button>
    </section>}

    <div className="grid">
      <section className="panel"><h2>Experiments</h2>
        {experiments.length===0&&<p className="empty">No experiment files found.</p>}
        {experiments.map(x=><div className="catalog" key={x.id}>
          <span className="catalog-id">{x.fault??'?'}</span>
          <div><strong>{x.id}</strong><p>{x.target} · {x.duration_s}s</p></div>
          <button className="primary" disabled={busy||running||execDisabled} title={execDisabled?'Execution disabled':running?'A run is active':'Run this experiment'} onClick={()=>start(x.id)}><Play size={15}/> Run</button>
        </div>)}
        {execDisabled&&<p className="muted">Execution disabled: the Run buttons stay grey until the API is started with CHAOS_EXECUTION_ENABLED=true.</p>}
      </section>
      <section className="panel"><h2>Runs</h2>
        {runs.length===0?<p className="empty">No saved runs yet.</p>:<div className="chaos-table-wrap"><table className="chaos-table"><thead><tr><th>Experiment</th><th>Fault</th><th>Target</th><th>Error rate</th><th>p95 (base → fault)</th><th>Recovery</th><th>Stop reason</th></tr></thead><tbody>
          {runs.map(r=><tr key={r.run_id} className={(r.recovered===false?'bad ':'')+(r.run_id===selected?'selected':'')} onClick={()=>openRun(r.run_id)} tabIndex={0} onKeyDown={e=>{if(e.key==='Enter')openRun(r.run_id);}}>
            <td>{r.experiment}</td><td>{r.fault??'n/a'}</td><td>{r.target}</td><td>{pct(r.during_error_rate)}</td><td>{ms(r.baseline_p95_ms)} → {ms(r.during_p95_ms)}</td>
            <td>{r.recovered===false?'NOT RECOVERED':secs(r.recovery_s)}</td><td>{r.stop_reason??'n/a'}</td></tr>)}
        </tbody></table></div>}
      </section>
    </div>

    {detail&&<RunDetail run={detail}/>}
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
    {name:'Baseline',x1:rel(t0),x2:rel(fs??(run.during[0]?.t??end)),fill:'#d9ecf2'},
    ...(fs!=null?[{name:'Fault',x1:rel(fs),x2:rel(fe??end),fill:'#f6d9d9'}]:[]),
    ...(fe!=null?[{name:'Recovery',x1:rel(fe),x2:rel(end),fill:'#dcefe0'}]:[]),
  ];
  const bad=!run.recovered;
  return <section className="panel chaos-detail" aria-label="Run detail">
    <div className="panel-title"><h2>{run.experiment.name} · <code>{run.run_id}</code></h2><span className={'small-tag'+(bad?' bad':'')}>{run.refused?'REFUSED':bad?'NOT RECOVERED':'RECOVERED'}</span></div>
    <p className="muted">{run.experiment.fault} on {run.experiment.target} for up to {run.experiment.duration_s}s · recovery {secs(run.recovery_s)}</p>
    <div className={'chaos-callout'+(run.stop_reason?.startsWith('safety')?' safety':'')} role="note"><strong>Stop reason</strong><span>{run.stop_reason??'none recorded'}</span></div>
    {all.length===0?<p className="empty">No probe samples were recorded (run refused before injecting).</p>:
    <div className="chaos-chart" role="img" aria-label="Latency over time with baseline, fault and recovery phases shaded"><ResponsiveContainer width="100%" height={300}>
      <ComposedChart data={points} margin={{top:10,right:20,bottom:20,left:0}}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e3ebed"/>
        {phases.map(p=><ReferenceArea key={p.name} x1={p.x1} x2={p.x2} fill={p.fill} fillOpacity={0.55} ifOverflow="visible" label={{value:p.name,position:'insideTop',fontSize:11,fill:'#456'}}/>)}
        <XAxis type="number" dataKey="x" domain={['dataMin','dataMax']} tickFormatter={v=>`${v}s`} label={{value:'seconds since first probe',position:'insideBottom',offset:-10,fontSize:11}}/>
        <YAxis label={{value:'latency (ms)',angle:-90,position:'insideLeft',fontSize:11}}/>
        <Tooltip formatter={(v:number,n:string)=>[`${v} ms`,n]}/>
        <Legend verticalAlign="top" height={28}/>
        <Line type="monotone" dataKey="latency" name="latency (ok probes)" stroke="#0b8178" strokeWidth={2} dot={{r:2}} connectNulls isAnimationActive={false}/>
        <Scatter dataKey="failed" name="failed probe" fill="#b14343" shape="cross" isAnimationActive={false}/>
      </ComposedChart>
    </ResponsiveContainer></div>}
    <dl className="chaos-summary">
      {(['baseline','during','recovery'] as const).map(k=><React.Fragment key={k}><dt>{k}</dt><dd>{run.summary[k].count} probes · {run.summary[k].failures} failed · p95 {ms(run.summary[k].p95_ms)}</dd></React.Fragment>)}
    </dl>
  </section>;
}
