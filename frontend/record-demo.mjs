// Record the real local prevention workflow. Never persist the operator token.
import {chromium} from '@playwright/test';
import {spawn} from 'node:child_process';
import {mkdir,writeFile,rename} from 'node:fs/promises';
import {randomBytes} from 'node:crypto';
import path from 'node:path';
const root=path.resolve('..');
const reuse=process.env.RECORD_EXISTING==='true';
const token=reuse?'browser-test-disposable-token-0000000000':randomBytes(32).toString('base64url');
const python=process.platform==='win32'?path.join(root,'.venv/Scripts/python.exe'):path.join(root,'.venv/bin/python');
const children=[];
const env={...process.env,DATA_MODE:'demo',OPERATOR_TOKEN:token,FAKE_RUNTIME_ENABLED:'false'};
const out=path.join(root,'outputs');
let browser;
try {
 for(const [app,port] of reuse?[]:[['app.demo_target:app','8090'],['app.main:create_app','8000']]){
  const args=['-m','uvicorn',app,'--host','127.0.0.1','--port',port,'--no-access-log','--log-level','critical'];
  if(app.endsWith('create_app'))args.push('--factory');
  children.push(spawn(python,args,{cwd:root,env,windowsHide:true,stdio:'ignore'}));
 }
 let ready=false;
 for(let i=0;i<50;i++){try{const r=await fetch('http://127.0.0.1:8000/api/v1/health',{headers:{Authorization:'Bearer '+token}});if(r.ok){ready=true;break;}}catch{}await new Promise(r=>setTimeout(r,100));}
 if(!ready)throw new Error('Demo API not ready; another process may own port 8000.');
 if(reuse)await fetch('http://127.0.0.1:8000/api/v1/safety/reset',{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:'{}'});
 await mkdir(out,{recursive:true});
 const before=await (await fetch('http://127.0.0.1:8090/health')).json();
 browser=await chromium.launch({channel:'chromium'});
 const context=await browser.newContext({viewport:{width:1440,height:1000},recordVideo:{dir:path.join(root,'work/video'),size:{width:1440,height:1000}}});
 const page=await context.newPage();
 await page.goto('http://127.0.0.1:8000');
 await page.getByLabel('Operator token').fill(token);
 await page.getByRole('button',{name:'Connect workspace'}).click();
 await page.getByRole('button',{name:'Inject test error'}).waitFor();
 await page.screenshot({path:path.join(out,'demo-overview.png'),fullPage:true});
 await page.waitForTimeout(3000);
 const response=page.waitForResponse(r=>r.url().endsWith('/api/v1/demo/injection'));
 await page.getByRole('button',{name:'Inject test error'}).click();
 const result=await (await response).json();
 await page.getByRole('alertdialog').waitFor();
 await page.screenshot({path:path.join(out,'demo-error-popup.png'),fullPage:true});
 await page.waitForTimeout(6500);
 await page.getByRole('button',{name:'Acknowledge error'}).click();
 await page.waitForTimeout(2000);
 await page.getByRole('button',{name:'Findings',exact:true}).click();
 await page.screenshot({path:path.join(out,'demo-findings.png'),fullPage:true});
 await page.waitForTimeout(4000);
 const after=await (await fetch('http://127.0.0.1:8090/health')).json();
 if(result.executed!==false || result.mutations_dispatched!==0 || result.rule_id!=='DURATION_HARD_LIMIT' || after.status!=='healthy' || after.mutations_received!==0)throw new Error('Local demo acceptance failed');
 const evidence={schema_version:'1.0',source:'live',scope:'local HTTP prevention demo; no Kubernetes execution',captured_at:new Date().toISOString(),target_before:before,guard_result:result,target_after:after,checks:{blocked_before_execution:true,zero_mutations:true,target_remained_healthy:true,popup_verified:true},limitations:['The local demo target is read-only; no fault executor is installed','No Kubernetes fault, SQL execution, model learning or resilience score was verified']};
 await writeFile(path.join(root,'reports/local-prevention-demo.json'),JSON.stringify(evidence,null,2)+'\n');
 await writeFile(path.join(out,'local-prevention-evidence.json'),JSON.stringify(evidence,null,2)+'\n');
 const video=page.video();await context.close();await video.saveAs(path.join(out,'local-prevention-demo.webm'));
 console.log('Real local demo recorded; BLOCK before execution, zero mutations, healthy target.');
}finally{if(browser)await browser.close();for(const child of children)child.kill();}
