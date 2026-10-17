import React,{useEffect,useState} from 'react';
import {CartesianGrid,ComposedChart,Legend,Line,ReferenceArea,ResponsiveContainer,Tooltip,XAxis,YAxis} from 'recharts';
import {chaos,type RunMetrics} from './chaosApi';
import {faultSpans,hasData,toRows} from './metricsUtil';

type Spec={title:string;names:{key:string;label:string;color:string}[];unit:string;step?:boolean;scale?:number};

const SPECS:Spec[]=[
  {title:'Independent probe vs controller probe (ms)',unit:'ms',names:[
    {key:'controller_latency_ms',label:'controller probe',color:'#0b8178'},
    {key:'probe_duration_ms',label:'blackbox probe',color:'#c57820'}]},
  {title:'Independent probe success (1 = ok)',unit:'',step:true,names:[{key:'probe_success',label:'blackbox success',color:'#456'}]},
  {title:'Target container network (packets/s sent)',unit:'pkt/s',names:[{key:'target_net_tx_pps',label:'tx packets/s',color:'#0b8178'}]},
  {title:'Target container memory (MiB)',unit:'MiB',scale:1/1048576,names:[{key:'target_memory_bytes',label:'working set',color:'#587179'}]},
  {title:'Target last seen (s ago; grows if it dies)',unit:'s',names:[{key:'target_last_seen_age_s',label:'seconds since last seen',color:'#b14343'}]},
];

/** Supporting views from Prometheus. The controller's own chart above stays the primary one. */
export function PrometheusCharts({runId}:{runId:string}){
  const [metrics,setMetrics]=useState<RunMetrics>();
  const [failure,setFailure]=useState('');
  const [loading,setLoading]=useState(true);

  useEffect(()=>{
    let stale=false;
    setMetrics(undefined);setFailure('');setLoading(true);
    chaos<RunMetrics>(`/runs/${encodeURIComponent(runId)}/metrics`)
      .then(m=>{if(!stale)setMetrics(m);})
      .catch(()=>{if(!stale)setFailure('Prometheus metrics could not be loaded for this run.');})
      .finally(()=>{if(!stale)setLoading(false);});
    return()=>{stale=true;};
  },[runId]);

  const note=(text:string)=><p className="prom-note" data-testid="prom-note">{text}</p>;
  let body:React.ReactNode;
  if(loading)body=note('Loading Prometheus metrics…');
  else if(failure)body=note(failure);
  else if(!metrics||!metrics.available)body=note(`No Prometheus metrics for this run${metrics?.reason?`: ${metrics.reason}`:'.'} The run record above is unaffected.`);
  else{
    const spans=faultSpans(metrics);
    body=<div className="prom-grid">{SPECS.map(spec=>{
      const names=spec.names.filter(n=>hasData(metrics,n.key));
      if(names.length===0)return null;
      const rows=toRows(metrics,names.map(n=>n.key)).map(r=>spec.scale?{...r,...Object.fromEntries(names.map(n=>[n.key,r[n.key]===undefined?undefined:(r[n.key] as number)*spec.scale!]))}:r);
      return <figure className="prom-chart" key={spec.title} aria-label={spec.title}>
        <figcaption>{spec.title}</figcaption>
        <ResponsiveContainer width="100%" height={170}>
          <ComposedChart data={rows} margin={{top:6,right:12,bottom:6,left:0}}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e3ebed"/>
            {spans.map((s,i)=><ReferenceArea key={i} x1={s.x1} x2={s.x2} fill="#f6d9d9" fillOpacity={0.5} ifOverflow="visible"/>)}
            <XAxis type="number" dataKey="x" domain={['dataMin','dataMax']} tickFormatter={v=>`${v}s`} fontSize={10}/>
            <YAxis fontSize={10} width={46} domain={spec.step?[0,1]:['auto','auto']}/>
            <Tooltip formatter={(v:number,n:string)=>[`${Math.round(v*100)/100} ${spec.unit}`.trim(),n]}/>
            {names.length>1&&<Legend verticalAlign="top" height={22} wrapperStyle={{fontSize:11}}/>}
            {names.map(n=><Line key={n.key} type={spec.step?'stepAfter':'linear'} dataKey={n.key} name={n.label} stroke={n.color} strokeWidth={1.8} dot={false} connectNulls isAnimationActive={false}/>)}
          </ComposedChart>
        </ResponsiveContainer>
      </figure>;
    })}</div>;
  }
  return <section className="prom" aria-label="Prometheus metrics">
    <h3>Supporting views from Prometheus</h3>
    <p className="muted">Observability only. Shaded areas are when the fault was active. Missing if the observability stack was not running during the run.</p>
    {body}
  </section>;
}
