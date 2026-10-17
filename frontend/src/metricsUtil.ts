import type {RunMetrics} from './chaosApi';

export type Row={x:number;[series:string]:number|undefined};

const num=(v:unknown):number|undefined=>{
  const n=typeof v==='string'?Number(v):v;
  return typeof n==='number'&&Number.isFinite(n)?n:undefined;
};

/** Merge the named series into one row per timestamp, with x as seconds since the window start. */
export function toRows(metrics:RunMetrics,names:string[]):Row[]{
  const rows=new Map<number,Row>();
  for(const name of names){
    for(const point of metrics.series[name]??[]){
      const t=num(point[0]),v=num(point[1]);
      if(t===undefined||v===undefined)continue;
      const x=Math.round((t-metrics.window.start)*100)/100;
      const row=rows.get(x)??{x};
      row[name]=v;
      rows.set(x,row);
    }
  }
  return [...rows.values()].sort((a,b)=>a.x-b.x);
}

/** Spans (in seconds since the window start) where ``fault_active`` was on. */
export function faultSpans(metrics:RunMetrics):{x1:number;x2:number}[]{
  const rows=toRows(metrics,['fault_active']);
  const spans:{x1:number;x2:number}[]=[];
  let start:number|undefined;
  let last=0;
  for(const r of rows){
    const on=(r.fault_active??0)>0.5;
    if(on&&start===undefined)start=r.x;
    if(!on&&start!==undefined){spans.push({x1:start,x2:r.x});start=undefined;}
    last=r.x;
  }
  if(start!==undefined)spans.push({x1:start,x2:last});
  return spans;
}

export const hasData=(metrics:RunMetrics,name:string)=>(metrics.series[name]??[]).length>0;
