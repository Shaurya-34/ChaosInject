import React,{useEffect,useState} from 'react';
import {chaos,type Score} from './chaosApi';

/** Yellow for good, pale lavender for middling, violet for poor. Yellow is the "act on this" colour of the theme, not a warning. */
export function scoreTone(score:number|null):string{
  if(score===null)return '#a3a1b8';
  if(score>=80)return '#ffe14a';
  if(score>=50)return '#cfc9ee';
  return '#a58bff';
}
const fmt=(x:number|null|undefined)=>x==null?'n/a':`${x}`;

/** Headline number in the header on every page. Polls /score; shows nothing if the API refuses. */
export function ScoreChip({onOpen}:{onOpen:()=>void}){
  const [score,setScore]=useState<Score>();
  useEffect(()=>{
    let stopped=false;
    const load=()=>chaos<Score>('/score').then(s=>{if(!stopped)setScore(s);}).catch(()=>{});
    load();
    const timer=setInterval(load,15000);
    return()=>{stopped=true;clearInterval(timer);};
  },[]);
  if(!score)return null;
  return <button className="score-chip" onClick={onOpen} aria-label={`Resilience score: ${score.headline}`} title={`Resilience score: ${score.headline}. Click for the breakdown.`} style={{borderColor:scoreTone(score.score),color:scoreTone(score.score)}}>
    <span className="score-chip-label">RESILIENCE</span><strong>{fmt(score.score)}</strong>{score.score!==null&&!score.complete&&<span className="score-chip-partial">partial</span>}
  </button>;
}

export function ScoreCard({score,onOpenRun}:{score:Score|undefined;onOpenRun:(id:string)=>void}){
  if(!score)return null;
  const cov=score.coverage;
  const pct=cov.fraction===null?0:Math.round(cov.fraction*100);
  const tone=scoreTone(score.score);
  return <section className="panel score-card" aria-label="Resilience score">
    <div className="score-head">
      <div className="score-number" style={{color:tone}} aria-label={`Resilience score ${fmt(score.score)}`}>{fmt(score.score)}<small>/100</small></div>
      <div>
        <h2>Resilience score</h2>
        <p className="score-headline">{score.headline}</p>
        <p className="muted">Deterministic, from saved runs only. Formula and weights: <code>docs/resilience-score.md</code>. A run that did not recover scores 0; a run ended by a safety stop is capped at {score.safety_stop_cap}.</p>
        <p className="muted score-legend">Colour: yellow is good (80 and up), pale lavender is middling (50 to 79), violet is poor (below 50).</p>
      </div>
    </div>
    <div className="score-coverage" aria-label="Coverage">
      <div className="score-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}><span style={{width:`${pct}%`}}/></div>
      <span>Coverage: {cov.tested} of {cov.possible} (target, fault) pairs tested</span>
    </div>
    {score.score===null&&<p className="empty">No scored runs yet. Start one from Experiments below, and the score appears here.</p>}
    {cov.untested.length>0&&<details><summary>{cov.untested.length} pairs not tested yet</summary><ul className="muted">{cov.untested.map(u=><li key={u.target+u.fault}>{u.target} · {u.fault}</li>)}</ul></details>}
    {score.targets.length+score.unmapped_targets.length>0&&<details className="score-breakdown"><summary>Breakdown by target and fault</summary>
      <table className="chaos-table"><thead><tr><th>Target</th><th>Fault</th><th>Score</th><th>Note</th><th>Scored run</th><th>All runs</th></tr></thead><tbody>
        {[...score.targets,...score.unmapped_targets].flatMap(t=>t.pairs.map((p,i)=><tr key={t.target+p.fault}>
          <td>{i===0?<>{t.target}<br/><span className="muted">target score {t.score}{'weight' in t&&t.weight!==undefined?` · weight ${t.weight}`:' · not in overall'}</span></>:''}</td>
          <td>{p.fault}</td><td><strong>{p.score}</strong></td>
          <td>{p.capped?`capped: ${p.cap_reason} (from ${p.uncapped_score})`:p.not_recovered?'not recovered → 0':''}</td>
          <td><button className="link" onClick={()=>onOpenRun(p.scored_run_id)}>{p.scored_run_id}</button></td>
          <td className="muted">{p.run_ids.length}</td></tr>))}
      </tbody></table>
      {score.runs_not_scored.length>0&&<p className="muted">{score.runs_not_scored.length} run(s) not scored (refused or incomplete).</p>}
    </details>}
  </section>;
}
