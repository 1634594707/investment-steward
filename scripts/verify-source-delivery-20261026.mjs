import { createRequire } from 'node:module';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';
const require=createRequire('C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/__noop.js');
const {chromium}=require('playwright-core');
const root=resolve('docs/assets/history-audit-2026-10-25/r05');
const out=resolve(root,'browser');mkdirSync(out,{recursive:true});
const names=['stock-news-url','stock-missing-news-url','collab-news-url','collab-missing-news-url'];
const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
const checks=[],errors=[];
const emptyObjects=new Set(['/overview','/personal/settings','/investor/profile','/brief/today','/review/weekly','/learning/unit/today','/learning/goals/current','/research/questions/latest','/quant/artifacts']);
try {
 for(const name of names) {
  const report=JSON.parse(readFileSync(resolve(root,`${name}.json`),'utf8')).report;
  const rows=report.source_catalog;
  const expectedLinks=rows.flatMap(row=>row.documents?.length?row.documents.map(doc=>doc.url).filter(Boolean):row.url?[row.url]:[]);
  const context=await browser.newContext();
  const page=await context.newPage();page.on('pageerror',e=>errors.push(String(e)));
  await page.addInitScript(()=>{localStorage.setItem('steward.last-view','workbench');localStorage.setItem('steward.onboarding-deferred-at',String(Date.now()));});
  await context.route('**/audit-core/**',async route=>{
   assert.equal(route.request().method(),'GET');
   const path=new URL(route.request().url()).pathname.slice('/audit-core'.length);
   let body=emptyObjects.has(path)?null:[];
   if(path==='/ai-research/reports')body={ok:true,items:[report],total:1,counts:{total:1,direction:0,stock:1,collab:0,draft_stock:0},models:[report.model]};
   if(path===`/ai-research/reports/${report.report_id}`)body={ok:true,item:report};
   await route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
  });
  for(const width of [390,768,1440]) {
   await page.setViewportSize({width,height:1000});await page.goto('http://127.0.0.1:5181/');
   await page.getByRole('heading',{name:'AI 研究工作台',exact:true}).waitFor();
   await page.locator('.wb-section-tab').last().click();await page.locator('.wb-history-open').first().click();
   await page.getByRole('tab',{name:'证据',exact:true}).click();
   const table=page.locator('.wb-source-catalog-table');await table.waitFor();
   const text=await table.innerText();
   const links=await table.locator('a').evaluateAll(els=>els.map(el=>el.getAttribute('href')));
   assert.deepEqual(links,expectedLinks);
   for(const row of rows) {
    const tr=table.locator('tbody tr').filter({has:page.locator('td:first-child',{hasText:new RegExp(`^${row.source_id}$`)})});
    assert.equal(await tr.locator('td').nth(5).innerText(),row.quality);
    for(const part of [row.name,row.scope_note,...(row.documents??[]).map(doc=>doc.title)])if(part)assert(text.includes(part));
   }
   const layout=await page.locator('#mainScroll').evaluate(el=>({client:el.clientWidth,scroll:el.scrollWidth}));
   assert(layout.scroll<=layout.client+1);
   await table.screenshot({path:resolve(out,`${name}-application-${width}.png`)});
   checks.push({name,mode:'application',width,links,layout});
  }
  await context.close();
  const offline=await browser.newContext({offline:true});
  // Intercept navigation solely to observe exact stored addresses; no external server is contacted.
  await offline.route('https://**/*',route=>route.fulfill({contentType:'text/plain',body:'Local acceptance destination'}));
  const file=await offline.newPage();file.on('pageerror',e=>errors.push(String(e)));
  for(const width of [390,768,1440]) {
   await file.setViewportSize({width,height:1000});await file.goto(pathToFileURL(resolve(root,`${name}.html`)).href);
   const text=await file.locator('body').innerText();
   const layout=await file.evaluate(()=>({client:innerWidth,scroll:document.documentElement.scrollWidth}));assert(layout.scroll<=width);
   for(const row of rows)for(const part of [row.name,row.scope_note,...(row.documents??[]).map(doc=>doc.title)])if(part)assert(text.includes(part));
   const external=file.locator('a[href^="https://"]');
   assert.deepEqual((await external.evaluateAll(els=>els.map(el=>el.getAttribute('href')))).sort(),[...expectedLinks].sort());
   let keyboardScrolls=0;
   for(const region of await file.locator('.doc-table-scroll').all()) {
    if(await region.evaluate(el=>el.scrollWidth>el.clientWidth)) {
     await region.focus();await file.keyboard.press('ArrowRight');await file.waitForTimeout(200);
     assert(await region.evaluate(el=>el.scrollLeft>0));keyboardScrolls++;
    }
   }
   await file.locator('.doc-source-catalog').screenshot({path:resolve(out,`${name}-offline-${width}.png`)});
   checks.push({name,mode:'offline',width,links:expectedLinks,keyboardScrolls,layout});
  }
  for(const link of await file.locator('a[href^="https://"]').all()) {
   const href=await link.getAttribute('href');await link.focus();
   const opened=offline.waitForEvent('page');await file.keyboard.press('Enter');const popup=await opened;
   await popup.waitForLoadState();assert.equal(popup.url(),href);await popup.close();
  }
  await file.emulateMedia({media:'print'});await file.evaluate(()=>document.fonts.ready);
  await file.pdf({path:resolve(out,`${name}-a4.pdf`),format:'A4',printBackground:true,margin:{top:'12mm',bottom:'12mm',left:'0mm',right:'0mm'}});
  writeFileSync(resolve(out,`${name}-print-text.txt`),await file.locator('body').innerText(),'utf8');
  await offline.close();
 }
 assert.deepEqual(errors,[]);
 writeFileSync(resolve(out,'checks.json'),JSON.stringify({checks,errors,scope:'Controlled HTTP and offline intercepted destinations; no external access. PDF generated; page inspection pending.'},null,2));
 console.log(`Passed ${checks.length} viewport checks; all stored source URLs activated by keyboard; four PDFs generated.`);
} finally {await browser.close();}
