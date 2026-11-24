const {chromium}=require('playwright-core');
const assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url');
const path=require('node:path');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CODS_BROWSER_PATH});
 try{
  const page=await browser.newPage({viewport:{width:1100,height:900},reducedMotion:'reduce'});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(pathToFileURL(path.resolve(__dirname,'../glyphs/preview.html')).href);
  const result=await page.evaluate(()=>{
   let clock=0;animator.now=()=>clock;animator.request=()=>null;animator.epoch=0;
   animator.setReducedMotion(false);animator.setExpression('annoyed');
   const snapshots=[0,160,320,480,640].map(t=>{
    clock=t;animator._draw(t);
    const rgba=animator.context.getImageData(0,0,96,112).data;
    return {pose:structuredClone(animator.lastPose),opaque:[...rgba].filter((_,i)=>i%4===3).every(a=>a===0||a===255),pixels:document.querySelector('#codsworth').toDataURL()};
   });
   animator.setExpression('pleased');clock=800;animator._draw(clock);
   const before=structuredClone(animator.lastPose);animator.setExpression('concerned');
   return {snapshots,before,after:animator.lastPose};
  });
  assert.equal(result.snapshots[0].pose.glare,0);
  assert.equal(result.snapshots.at(-1).pose.glare,1);
  assert.ok(result.snapshots[2].pose.glare>0&&result.snapshots[2].pose.glare<1);
  assert.equal(new Set(result.snapshots.map(s=>s.pixels)).size,5);
  assert.ok(result.snapshots.every(s=>s.opaque));
  assert.deepEqual(result.before,result.after);
  assert.deepEqual(errors,[]);
  console.log('Browser: five distinct opaque transition frames, progressive shutters and continuous retargeting passed.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
