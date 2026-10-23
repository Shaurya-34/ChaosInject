import {test,expect} from '@playwright/test';

// Runs against the disposable server from scripts/browser-test-server.py: the real API and the built dashboard.
const token='browser-test-disposable-token-0000000000';

test('small-screen layout keeps Emergency STOP reachable and the page does not scroll sideways',async({page})=>{
  await page.setViewportSize({width:390,height:844});await page.goto('/');
  await page.getByLabel('Operator token').fill(token);await page.getByRole('button',{name:'Connect workspace'}).click();
  const stop=page.getByRole('button',{name:'Emergency STOP'});
  await expect(stop).toBeVisible();await expect(stop).toBeEnabled();
  const box=await stop.boundingBox();expect(box!.x+box!.width).toBeLessThanOrEqual(390);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth)).toBe(false);
});