import { createRequire } from 'node:module';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import assert from 'node:assert/strict';
const require = createRequire('C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/__noop.js');
const { chromium } = require('playwright-core');
const root = resolve('docs/assets/history-audit-2026-10-25');
const report = JSON.parse(readFileSync(resolve(root,'original-report-view.json'),'utf8'));
const out = resolve(root,'application'); mkdirSync(out,{recursive:true});
const objects = new Set(['/overview','/personal/settings','/investor/profile','/brief/today','/review/weekly','/learning/unit/today','/learning/goals/current','/research/questions/latest','/quant/artifacts']);
const browser = await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
const errors=[],calls=[];
try {
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 page.on('pageerror',e=>errors.push(String(e)));
 await page.addInitScript(()=>{ localStorage.setItem('steward.last-view','workbench'); localStorage.setItem('steward.onboarding-deferred-at',String(Date.now())); });
 await page.route('**/audit-core/**',async route=>{
  const path=new URL(route.request().url()).pathname.slice('/audit-core'.length); calls.push(path);
  if(route.request().method()!=='GET') throw new Error('Unexpected write '+path);
  let body=objects.has(path)?null:[]; let status=200;
  if(path==='/portfolio/risk'||path==='/research/templates'){status=503;body={detail:'Not supplied by this read-only browser fixture'};}
  if(path==='/ai-research/reports')body={ok:true,items:[{...report,kind:'stock',subject:report.symbol}],total:1,counts:{total:1,direction:0,stock:1,collab:0,draft_stock:0},models:[report.model]};
  if(path===`/ai-research/reports/${report.report_id}`)body={ok:true,item:{...report,kind:'stock',subject:report.symbol}};
  await route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
 });
 const initialReadingChecks=[];
 for(const width of [1440,768,390]) {
  await page.setViewportSize({width,height:1000});
  await page.goto('http://127.0.0.1:5181/');
  await page.getByRole('heading',{name:'AI 研究工作台',exact:true}).waitFor();
  await page.locator('.wb-section-tab').last().click();
  await page.locator('.wb-history-open').first().click();
  const title=page.getByText('阅读前核对',{exact:true});
  await title.waitFor();
  const card=await title.locator('..').boundingBox();
  const scrollTop=await page.locator('#mainScroll').evaluate(el=>el.scrollTop);
  const lines=await title.locator('..').locator('li').allTextContents();
  const mainBox=await page.locator('#mainScroll').boundingBox();
  const topbarBox=await page.locator('#mainScroll .topbar').boundingBox();
  const visibleTop=Math.max(mainBox.y,topbarBox.y+topbarBox.height);
  const reportTitle=await page.getByRole('heading',{name:report.title,exact:true}).boundingBox();
  assert(reportTitle && reportTitle.y >= visibleTop-1, 'History report title must remain visible below sticky toolbar');
  assert(card && card.y >= visibleTop && card.y + card.height <= Math.min(1000,mainBox.y+mainBox.height), `Full reading card must be visible on history open at ${width}: ${JSON.stringify(card)}`);
  assert(lines.some(line=>line.includes('A 级来源：0 条')));
  assert(lines.some(line=>line.includes('下一验证动作：')));
  initialReadingChecks.push({width,card,reportTitle,visibleTop,scrollTop,lines});
  await page.screenshot({path:resolve(out,`initial-reading-summary-${width}.png`)});
 }
 writeFileSync(resolve(out,'initial-reading-checks.json'),JSON.stringify(initialReadingChecks,null,2));
 console.log('Initial reading checks:',JSON.stringify(initialReadingChecks));
 const checks=[];
 const md=readFileSync(resolve(root,'original-report-reexport.md'),'utf8');
 for(const width of [1440,768,390]) {
  await page.setViewportSize({width,height:1000});
  for(const tab of ['概览','正文','证据']) {
   await page.getByRole('tab',{name:tab,exact:true}).click();
   const text=await page.locator('body').innerText();
   if(tab==='概览') {
    assert(text.includes('研究完成度：1/4'));
    assert(text.includes('合理价值尚未评估'));
    assert(text.includes('数据超期（C4）'));
    assert(!text.includes('pe_ttm 14.37'));
    assert(text.includes('PE(TTM) 14.37'));
   }
   if(tab==='正文') assert(text.includes(report.report.split('\n').find(line=>line.trim()).replace(/^#+\s*/,'')));
   if(tab==='证据') {
    for(const line of ['不足以判断盈利质量','未给出可复算的贡献比例，不补填数值','模型列出 5 只，同业快照采用 38 只','PB（MRQ） 60 只、PE（TTM） 38 只、PS（TTM） 60 只']) {
     assert(text.includes(line),`Missing application text: ${line}`);
     assert(md.includes(line),`Missing Markdown text: ${line}`);
    }
    assert(!text.includes(report.research_completeness.note));
    assert(!text.includes(report.valuation_divergence.comparable_review.count_conflict));
    for(const label of ['完成度计算留痕','可比样本计算留痕']) {
     const summary=page.getByText(label,{exact:true});await summary.click();
     const expanded=await page.locator('body').innerText();
     assert(expanded.includes(label==='完成度计算留痕'?report.research_completeness.note:report.valuation_divergence.comparable_review.note));
     await summary.click();
    }
   }
   const layout=await page.evaluate(()=>{const main=document.getElementById('mainScroll');return {mainWidth:main.clientWidth,mainScrollWidth:main.scrollWidth,mainScrollLeft:main.scrollLeft,documentWidth:document.documentElement.scrollWidth};});
   if(layout.mainScrollWidth>layout.mainWidth+1) console.log(await page.evaluate(()=>[...document.querySelectorAll('#mainScroll *')].filter(el=>el.clientWidth>0&&el.scrollWidth>el.clientWidth+5).map(el=>({cls:el.className,client:el.clientWidth,scroll:el.scrollWidth,width:el.getBoundingClientRect().width})).slice(0,22)));
   assert(layout.mainScrollWidth<=layout.mainWidth+1,`Main content overflows at ${width}/${tab}: ${JSON.stringify(layout)}`);
   assert.equal(layout.mainScrollLeft,0);
   await page.screenshot({path:resolve(out,`${tab}-${width}.png`),fullPage:true});
   checks.push({width,tab,textAssertionsPassed:true,...layout});
  }
 }
 assert.equal(errors.length,0);
 writeFileSync(resolve(out,'checks.json'),JSON.stringify({checks,errors,initialReadingChecks,scope:'Actual AppShell and history-open navigation using archived HTTP-view fixture; other endpoints are controlled test responses. No live backend, external data truth, or installed-desktop acceptance.'},null,2));
 await page.screenshot({path:resolve(out,'boot.png'),fullPage:true});
 writeFileSync(resolve(out,'boot.json'),JSON.stringify({errors,calls,text:await page.locator('body').innerText(),scope:'Real AppShell with controlled read-only HTTP responses; no live backend or user database'},null,2));
 console.log(JSON.stringify({checks,errors},null,2));
} finally {await browser.close();}
