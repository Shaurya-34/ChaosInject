import React from 'react';
import type {Environment} from './chaosApi';

type Item={id:string;level:'blocker'|'info';title:string;detail:string;command?:string};

/** What stands between a fresh clone and a first run, from GET /environment. Empty when everything is ready. */
export function checklistItems(env:Environment):Item[]{
  const items:Item[]=[];
  if(!env.execution_enabled){
    items.push({id:'execution',level:'blocker',title:'Execution is disabled',
      detail:'You can read everything, but Run, Stop and Recover stay grey. To run experiments, restart with execution enabled:',
      command:'python scripts/dev.py down && python scripts/dev.py up --execute'});
  }
  const sandbox=env.sandbox;
  if(sandbox.reachable===false){
    items.push({id:'sandbox',level:'blocker',title:'The hotel sandbox is not running',
      detail:`Nothing answers at ${sandbox.url??'the sandbox address'}, so an experiment would be refused before it starts. Start the sandbox:`,
      command:env.execution_enabled?'python scripts/dev.py up --execute':'python scripts/dev.py up'});
  }else if(sandbox.reachable===true&&(sandbox.status??0)>=500){
    items.push({id:'sandbox-unhealthy',level:'blocker',title:`The hotel sandbox answers with an error (HTTP ${sandbox.status})`,
      detail:'The app is not healthy, so the controller would refuse to inject. Check the containers, or restart the sandbox:',
      command:'python scripts/dev.py status'});
  }
  if(env.prometheus.reachable===false){
    items.push({id:'prometheus',level:'info',title:'Prometheus is not running',
      detail:'Experiments still work. Only the supporting charts under a run are missing. This starts it together with the sandbox:',
      command:'python scripts/dev.py up'});
  }
  if(env.runs_saved===0){
    items.push({id:'no-runs',level:'info',title:'No runs yet',
      detail:'Start one from Experiments below. The score, findings and charts appear after the first run.'});
  }
  return items;
}

export function FirstRunChecklist({env}:{env:Environment|undefined}){
  if(!env)return null;
  const items=checklistItems(env);
  if(items.length===0)return null;
  return <section className="panel firstrun" aria-label="Getting started">
    <div className="panel-title"><h2>Getting started</h2><span className="small-tag">{items.some(i=>i.level==='blocker')?'ACTION NEEDED':'FOR INFORMATION'}</span></div>
    <ul>{items.map(i=><li key={i.id} className={'firstrun-item '+i.level} data-item={i.id}>
      <strong>{i.title}</strong>
      <p>{i.detail}</p>
      {i.command&&<code className="firstrun-command">{i.command}</code>}
    </li>)}</ul>
  </section>;
}
