import type {components} from './generated';
export type Model<K extends keyof components['schemas']> = components['schemas'][K];
let operatorToken = '';
export function authorize(token:string){operatorToken=token;}
export function currentToken(){return operatorToken;}
export async function request<T>(path:string, body?:unknown, headers:Record<string,string>={}):Promise<T>{
  const response=await fetch('/api/v1'+path,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+operatorToken,...(body===undefined?{}:{'Content-Type':'application/json'}),...headers},body:body===undefined?undefined:JSON.stringify(body),cache:'no-store'});
  if(!response.ok){const error=await response.json().catch(()=>({message:'Service unavailable'}));throw new Error(error.message || 'Operation refused');}
  return response.json();
}
export async function download(runId:string){
  const r=await fetch('/api/v1/reports/'+encodeURIComponent(runId),{headers:{Authorization:'Bearer '+operatorToken},cache:'no-store'});
  if(!r.ok)throw new Error('Report evidence unavailable');
  const url=URL.createObjectURL(await r.blob());const a=document.createElement('a');a.href=url;a.download='resilience-findings.html';a.click();URL.revokeObjectURL(url);
}
