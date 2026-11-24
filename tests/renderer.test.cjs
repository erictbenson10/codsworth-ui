const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const path = require('node:path');
const test = require('node:test');
const renderer = require('../glyphs/renderer.js');

// Independent Python export is the appearance contract: fractional poses must
// preserve the same geometry and transparent background as the offline assets.
const poses = [
  {},
  {bob:2,arms:[-2,2,0],lp:2,rp:2,apertures:[.75,.75],reach:-6,lift:2,grip:.85,glare:1},
  {bob:-1.35,arms:[-1.2,.8,.4],lp:-.75,rp:1.3,apertures:[.64,.18],gaze:.6,reach:-3.4,lift:8.7,grip:.42,tilt:1.7,glare:.38,turn:.21,flame:.8,voice:1.6},
  {bob:.5,arms:[.5,-.5,1.5],lp:.5,rp:-.5,apertures:[.92,.4],reach:1.5,lift:4.5,grip:.5,tilt:-2.5,glare:.73,turn:.9,flame:-1.5,voice:.5},
];
const script = `import sys,json\nsys.path.insert(0,'glyphs')\nimport codsworth as c\nposes=json.loads(sys.stdin.read())\noutput=[]\nfor pose in poses:\n    output.append([channel for row in c.frame(**pose) for pixel in row for channel in (*c.PALETTE[pixel],0 if pixel==c.BG else 255)])\nprint(json.dumps(output))`;
let expected;
test.before(() => { expected=JSON.parse(execFileSync('python3',['-c',script],{cwd:path.resolve(__dirname,'..'),input:JSON.stringify(poses),maxBuffer:8*1024*1024})); });
for(let i=0;i<poses.length;i++) test(`procedural pose ${i} exactly matches the native export`,()=>{
  const actual=renderer.render(poses[i]);
  assert.equal(actual.length,renderer.width*renderer.height*4);
  let mismatches=0,first=-1;
  for(let j=0;j<actual.length;j++)if(actual[j]!==expected[i][j]){mismatches++;if(first<0)first=j;}
  assert.equal(mismatches,0,`${mismatches} channel differences, first at pixel (${Math.floor(first/4)%96}, ${Math.floor(first/384)})`);
});
test('inbetween pose renders an actual intermediate articulated shape',()=>{
  const start=renderer.render(poses[0]),end=renderer.render(poses[1]),middle=renderer.render({...poses[1],bob:1,arms:[-1,1,0],lp:1,rp:1,apertures:[.575,.575],reach:-3,lift:1,grip:.425,glare:.5});
  assert.notDeepEqual(middle,start);assert.notDeepEqual(middle,end);
  for(let i=3;i<middle.length;i+=4)assert.ok(middle[i]===0||middle[i]===255,'transition pixels stay opaque or transparent; no double-image crossfade');
});
test('intermediate expression and activity poses stay inside the canvas',()=>{
 const catalog=require('../glyphs/animations/manifest.json');
 const clips=Object.entries(catalog.clips).filter(([,clip])=>clip.poses);
 const mix=(a,b,t)=>Array.isArray(b)?b.map((v,i)=>mix(a[i],v,t)):Number(a)+(Number(b)-Number(a))*t;
 for(const [fromName,from] of clips)for(const [toName,to] of clips){
  for(const t of [.25,.5,.75]){
   const a={...from.poses[0]},b={...to.poses[16]};
   a.bob+=fromName.startsWith('annoyed-')?4:0;
   b.bob+=toName.startsWith('annoyed-')?4:0;
   const pose=Object.fromEntries(Object.keys(b).map(k=>[k,mix(a[k],b[k],t)]));
   const rgba=renderer.render(pose);
   for(let x=0;x<96;x++)assert.equal(rgba[(x)*4+3]+rgba[((111*96)+x)*4+3],0,`${fromName} -> ${toName}: vertical clipping`);
   for(let y=0;y<112;y++)assert.equal(rgba[(y*96)*4+3]+rgba[(y*96+95)*4+3],0,`${fromName} -> ${toName}: horizontal clipping`);
  }
 }
});
