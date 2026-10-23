import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Activity,ShieldCheck,OctagonX,Lock} from 'lucide-react';
import {authorize} from './api';
import {ChaosRuns} from './ChaosRuns';
import {chaos,ChaosApiError} from './chaosApi';
import {ScoreChip} from './Score';
import './style.css';
import './chaos.css';
import './theme.css';

function App(){
 const [token,setToken]=useState(''),[connected,setConnected]=useState(false),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false);
 async function action(fn:()=>Promise<void>){setBusy(true);try{await fn();}catch(e){setNotice(e instanceof Error?e.message:'Operation refused');}finally{setBusy(false);}}
 async function connect(e:React.FormEvent){e.preventDefault();authorize(token);await action(async()=>{await chaos('/environment');setToken('');setConnected(true);});}
 async function stop(){
  try{await chaos('/active/stop','POST');setNotice('Stop requested. The controller stops the run, restores the fault and verifies recovery; watch the live panel.');}
  catch(e){
   if(e instanceof ChaosApiError&&e.code==='CONFLICT')setNotice('No run is active, so there is nothing to stop.');
   else if(e instanceof ChaosApiError&&e.code==='MUTATIONS_DISABLED')setNotice('This API has execution disabled, so it cannot stop a run. If a run is going in a terminal, press Ctrl+C there, or run: python -m chaos.controller --recover');
   else throw e;
  }
 }
 return <div className="app"><aside><div className="brand"><span className="brand-icon"><Activity size={22}/></span><div>CHAOSINJECT<span>RESILIENCE WORKBENCH</span></div></div><div className="workspace-label">LOCAL SANDBOX</div><nav><button className="selected"><Activity size={19}/>Chaos runs</button></nav><div className="aside-bottom"><ShieldCheck size={24}/><strong>Safety is deterministic</strong><p>Limits and STOP are enforced by the controller, never by AI reasoning.</p><span className="version">v1.0</span></div></aside><main><header><div className="context"><span className="dot"/>{connected?'Local sandbox':'Not connected'}{connected&&<ScoreChip onOpen={()=>document.querySelector('.score-card')?.scrollIntoView({behavior:'smooth'})}/>}</div><button className="stop" disabled={!connected} onClick={()=>action(stop)}><OctagonX size={17}/> Emergency STOP</button></header>
 {!connected?<section className="login"><span className="eyebrow">LOCAL OPERATOR ACCESS</span><h1>Understand impact.<br/>Keep control.</h1><p>Connect to the local API with your operator token. It stays in memory for this session.</p><form onSubmit={connect}><label htmlFor="token">Operator token</label><input id="token" type="password" autoComplete="off" value={token} onChange={e=>setToken(e.target.value)} required/><button className="primary" disabled={busy}><Lock size={16}/> Connect workspace</button></form>{notice&&<p role="alert">{notice}</p>}</section>:<>
 <section className="content"><div className="page-heading"><div><span className="eyebrow">REAL RUNS</span><h1>Chaos runs</h1><p>Start, watch and review real chaos experiments.</p></div></div>
 {notice&&<div role="status" className="notice">{notice}<button aria-label="Dismiss status" onClick={()=>setNotice('')}>×</button></div>}
 <ChaosRuns/>
 </section></>}
 </main></div>;
}
createRoot(document.getElementById('root')!).render(<App/>);
