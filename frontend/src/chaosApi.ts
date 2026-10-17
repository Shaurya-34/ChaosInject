import {currentToken} from './api';

// Shapes of /api/v1/chaos responses (contracts/run.schema.json and the router in backend/app/api/chaos_runs.py).
export type Sample={t:number;ok:boolean;status:number|null;latency_ms:number};
export type PhaseSummary={count:number;failures:number;error_rate:number|null;p50_ms:number|null;p95_ms:number|null;max_ms:number|null};
export type ChaosRun={
  run_id:string;saved_at:number;
  experiment:{name:string;target:string;fault:string;duration_s:number;[k:string]:unknown};
  baseline:Sample[];during:Sample[];recovery:Sample[];
  stop_reason:string|null;refused?:boolean;recovered:boolean;recovery_s:number|null;
  fault_started:number|null;fault_ended:number|null;
  summary:{baseline:PhaseSummary;during:PhaseSummary;recovery:PhaseSummary};
};
export type IndexEntry={
  run_id:string;file:string;experiment:string|null;target:string|null;fault?:string|null;duration_s?:number|null;
  fault_started?:number|null;fault_ended?:number|null;stop_reason:string|null;recovered:boolean|null;recovery_s:number|null;
  during_error_rate?:number|null;during_p95_ms?:number|null;baseline_p95_ms?:number|null;
};
export type ExperimentRow={id:string;name:string|null;target:string|null;fault:string|null;duration_s:number|null};
export type ActiveRun={
  running:boolean;experiment:string|null;pid:number|null;started_at:number|null;exit_code:number|null;run_id:string|null;
  fault_active:boolean;fault:{target?:string;fault?:string}|null;stop_requested:boolean;log_tail:string[];
  /** False when the API runs without CHAOS_EXECUTION_ENABLED; absent on older servers. */
  execution_enabled?:boolean;
};

export type Proposal={
  experiment:{name:string;target:string;fault:string;duration_s:number;[k:string]:unknown};
  rationale:string;expected_impact:string;affected_endpoints:string[];times_run:number;score:number;rationale_source:string;rationale_model?:string|null;
};
/** Where the model-written text comes from (mode is deterministic when no language model is configured). */
export type LlmInfo={mode:string;model:string|null;location:string|null};
export type Proposals={proposals:Proposal[];rejected:{target:string;fault:string;reason:string}[];limitations:string[];llm?:LlmInfo};

export type Finding={
  id:string;severity:number;severity_label:string;title:string;target:string;fault:string;affected_endpoints:string[];
  explanation:string;suggested_fix:string;run_ids:string[];explanation_source:string;explanation_model?:string|null;
  evidence:{run_id:string;stop_reason:string|null;recovered:boolean|null;recovery_s:number|null;baseline_p95_ms:number|null;during_p95_ms:number|null;during_error_rate:number|null}[];
};
export type Findings={findings:Finding[];runs_analyzed:number;runs_without_findings:number;runs_not_evaluated:number;limitations:string[];llm?:LlmInfo};

/** GET /runs/{id}/metrics: named Prometheus series over the run window (docs/observability.md). */
export type RunMetrics={
  window:{start:number;end:number};step_s:number;
  series:Record<string,[number|string,number|string][]>;
  available:boolean;reason:string|null;
};

export type ScorePair={
  fault:string;score:number;uncapped_score:number;capped:boolean;cap_reason:string|null;not_recovered:boolean;
  scored_run_id:string;run_ids:string[];
  inputs:{recovered:boolean;error_rate:number|null;baseline_p95_ms:number|null;during_p95_ms:number|null;recovery_s:number|null;recovery_timeout_s:number;stop_reason:string|null};
};
export type ScoreTarget={target:string;node:string|null;score:number;criticality?:number;weight?:number;pairs:ScorePair[]};
/** GET /score (docs/resilience-score.md). score is null when no run could be scored. */
export type Score={
  formula_version:number;score:number|null;headline:string;complete:boolean;
  coverage:{tested:number;possible:number;fraction:number|null;untested:{target:string;fault:string}[]};
  weights:Record<string,number>;safety_stop_cap:number;targets:ScoreTarget[];unmapped_targets:ScoreTarget[];
  runs_not_scored:{run_id:string;reason:string}[];
};

/** GET /environment: what a first run needs (null means "not probed", e.g. no hotel experiment on this machine). */
export type Environment={
  execution_enabled:boolean;
  sandbox:{url:string|null;reachable:boolean|null;status:number|null};
  prometheus:{reachable:boolean|null;status:number|null};
  runs_saved:number;checked_at:number;
};

export class ChaosApiError extends Error{
  constructor(public status:number,public code:string,message:string){super(message);}
}

export async function chaos<T>(path:string,method:'GET'|'POST'='GET'):Promise<T>{
  const response=await fetch('/api/v1/chaos'+path,{method,headers:{Authorization:'Bearer '+currentToken()},cache:'no-store'});
  if(!response.ok){
    const body=await response.json().catch(()=>({}));
    throw new ChaosApiError(response.status,body.code??'UNKNOWN',body.message??'Service unavailable');
  }
  return response.json();
}

/** Label for the source of an explanation: the model that wrote it, or the rule-based text. */
export function explainedBy(source:string|undefined,model:string|null|undefined,location?:string|null):string{
  if(!source||source==='deterministic'||!model)return 'Rule-based explanation';
  const where=location==='local'?'local':location==='hosted'?'hosted':source;
  return `Explained by ${model} (${where})`;
}

/** Human wording for a refused start/stop/recover call. */
export function refusalMessage(e:unknown):string{
  if(e instanceof ChaosApiError){
    if(e.code==='MUTATIONS_DISABLED')return 'Execution disabled: the API runs with CHAOS_EXECUTION_ENABLED off.';
    if(e.code==='CONFLICT')return 'A run is already active (or a fault is still in place).';
    if(e.code==='POLICY_REFUSED')return 'Refused by policy (chaos/policy.json).';
    return e.message;
  }
  return e instanceof Error?e.message:'Operation refused';
}

/** Phase of an in-flight run, read from the controller's log tail. */
export function phaseFromLog(lines:string[]):string{
  for(const line of [...lines].reverse()){
    if(line.includes('SAFETY STOP')||line.includes('KILL SWITCH'))return 'Stopping';
    if(line.includes('[recovery')||line.includes('restored '))return 'Recovery';
    if(line.includes('[fault')||line.includes('injecting '))return 'Fault active';
    if(line.includes('[baseline'))return 'Baseline';
  }
  return 'Starting';
}

/** Fetch a report with the operator token and hand it to the browser as a file download. */
export async function chaosDownload(path:string,filename:string):Promise<void>{
  const response=await fetch('/api/v1/chaos'+path,{headers:{Authorization:'Bearer '+currentToken()},cache:'no-store'});
  if(!response.ok)throw new ChaosApiError(response.status,'UNKNOWN','Report unavailable');
  const url=URL.createObjectURL(await response.blob());
  const a=document.createElement('a');a.href=url;a.download=filename;a.click();URL.revokeObjectURL(url);
}
