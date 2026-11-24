const {chromium} = require('playwright-core');
const assert = require('node:assert/strict');
const {pathToFileURL} = require('node:url');
const path = require('node:path');
(async () => {
 const browser = await chromium.launch({headless:true, executablePath:process.env.CODS_BROWSER_PATH});
 try {
  const page = await browser.newPage({viewport:{width:1050,height:850}});
  const errors=[]; page.on('pageerror', e=>errors.push(e.message));
  await page.goto(pathToFileURL(path.resolve(__dirname,'../glyphs/actions.html')).href);
  for (const [action,card,x] of [['open-panel','panel',460],['present-result','result',440],['inspect-error','error-card',500]]) {
   await page.locator(`[data-action="${action}"]`).click();
   await page.waitForFunction(()=>document.querySelector('#world').dataset.contact==='true');
   assert.equal(await page.locator(`#${card}`).isVisible(),true);
   await page.waitForFunction(()=>document.querySelector('#world').dataset.phase==='complete');
   assert.equal(await page.locator(`#${card}`).evaluate(el=>new DOMMatrix(getComputedStyle(el).transform).m41),x);
  }
  if(process.env.CODS_SCREENSHOT) await page.screenshot({path:process.env.CODS_SCREENSHOT});
  await page.locator('[data-action="open-panel"]').click();
  await page.locator('#reset-scene').click();
  await page.waitForTimeout(4000);
  assert.equal(await page.locator('#world').getAttribute('data-phase'),'idle');
  assert.equal(await page.locator('#panel').isVisible(),false);
  await page.locator('#scene-still').check();
  await page.locator('[data-action="present-result"]').click();
  await page.waitForFunction(()=>document.querySelector('#world').dataset.phase==='complete');
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  assert.deepEqual(errors,[]);
  console.log('Browser: three DOM scenes, contact, completion, cancellation, reduced motion and mobile layout passed.');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
