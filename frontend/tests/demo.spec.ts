import {test,expect} from '@playwright/test';
const token='browser-test-disposable-token-0000000000';
test('real HTTP target stays healthy; injection is blocked and popup is shown',async({page,request})=>{
 const before=await request.get('http://127.0.0.1:8290/health');expect(before.ok()).toBeTruthy();expect((await before.json()).status).toBe('healthy');
 await page.goto('/');await page.getByLabel('Operator token').fill(token);await page.getByRole('button',{name:'Connect workspace'}).click();
 await expect(page.getByText('Local prevention demo · real HTTP target')).toBeVisible();
 await expect(page.getByRole('button',{name:'Emergency STOP'})).toBeEnabled();
 const responsePromise=page.waitForResponse(r=>r.url().endsWith('/api/v1/demo/injection'));
 await page.getByRole('button',{name:'Check duration'}).click();const result=await (await responsePromise).json();
 expect(result.rule_id).toBe('DURATION_HARD_LIMIT');expect(result.executed).toBe(false);expect(result.mutations_dispatched).toBe(0);
 const dialog=page.getByRole('alertdialog');await expect(dialog).toBeVisible();await expect(dialog.getByRole('heading',{name:'Error has occurred'})).toBeVisible();await expect(dialog.getByText('Injection blocked before execution.',{exact:true})).toBeVisible();
 const after=await request.get('http://127.0.0.1:8290/health');const target=await after.json();expect(target.status).toBe('healthy');expect(target.mutations_received).toBe(0);
 await page.getByRole('button',{name:'Acknowledge error'}).click();await expect(dialog).not.toBeVisible();
 expect(await page.evaluate(()=>({local:localStorage.length,session:sessionStorage.length}))).toEqual({local:0,session:0});
 await page.getByRole('button',{name:'Findings',exact:true}).click();await expect(page.getByRole('heading',{name:'Duration check evidence'})).toBeVisible();await expect(page.getByText('DURATION_HARD_LIMIT',{exact:true})).toBeVisible();
 await page.reload();await page.getByLabel('Operator token').fill(token);await page.getByRole('button',{name:'Connect workspace'}).click();
 await expect(page.getByText('STOP LATCHED',{exact:true})).toBeVisible();await expect(page.getByRole('button',{name:'Check duration'})).toBeDisabled();
 await page.getByRole('button',{name:'Reset demo after health check'}).click();await expect(page.getByRole('button',{name:'Check duration'})).toBeEnabled();
});
test('SQL input clears and is never echoed as execution evidence',async({page})=>{
 await page.goto('/');await page.getByLabel('Operator token').fill(token);await page.getByRole('button',{name:'Connect workspace'}).click();
 await page.getByRole('button',{name:'Operations',exact:true}).click();await page.getByRole('button',{name:'Guarded SQL'}).click();
 const editor=page.getByLabel('Transient query editor');await editor.fill('SELECT PRIVATE_TRANSIENT_VALUE');await page.getByRole('button',{name:'Analyze query'}).click();
 await expect(editor).toHaveValue('');await expect(page.getByText(/Rules: SQL-.*Executed: no/)).toBeVisible();expect(await page.locator('body').innerText()).not.toContain('PRIVATE_TRANSIENT_VALUE');
});
test('small-screen layout keeps STOP and demo button reachable',async({page})=>{
 await page.setViewportSize({width:390,height:844});await page.goto('/');await page.getByLabel('Operator token').fill(token);await page.getByRole('button',{name:'Connect workspace'}).click();
 await expect(page.getByRole('button',{name:'Emergency STOP'})).toBeVisible();await expect(page.getByRole('button',{name:'Check duration'})).toBeVisible();
 const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth);expect(overflow).toBe(false);
});

 test('user durations show success at 60 and error at 61 without an outage',async({page,request})=>{
 await page.goto('/');await page.getByLabel('Operator token').fill(token);await page.getByRole('button',{name:'Connect workspace'}).click();
 const duration=page.getByLabel('Outage duration (seconds)');
 const check=page.getByRole('button',{name:'Check duration'});
 for(const seconds of [1,30,60]){
  await duration.fill(String(seconds));const pending=page.waitForResponse(r=>r.url().endsWith('/api/v1/demo/injection'));await check.click();const result=await (await pending).json();
  expect(result.duration_seconds).toBe(seconds);expect(result.decision).toBe('ALLOW');expect(result.executed).toBe(false);expect(result.stop_latched).toBe(false);expect(result.mutations_dispatched).toBe(0);
  await expect(page.getByRole('alertdialog').getByRole('heading',{name:'Safety check passed'})).toBeVisible();await expect(page.getByRole('alertdialog').getByText('Success means validation passed. No outage was executed.',{exact:true})).toBeVisible();await page.getByRole('button',{name:'Continue',exact:true}).click();
 }
 await duration.fill('0');await expect(check).toBeDisabled();await expect(page.getByText('Enter a whole number from 1 to 600.')).toBeVisible();
 await duration.fill('1.5');await expect(check).toBeDisabled();
 await duration.fill('61');await check.click();await expect(page.getByRole('alertdialog').getByRole('heading',{name:'Error has occurred'})).toBeVisible();
 await page.getByRole('button',{name:'Acknowledge error'}).click();await expect(check).toBeDisabled();await page.getByRole('button',{name:'Reset demo after health check'}).click();await expect(check).toBeEnabled();
 const target=await (await request.get('http://127.0.0.1:8290/health')).json();expect(target.status).toBe('healthy');expect(target.mutations_received).toBe(0);
 });
