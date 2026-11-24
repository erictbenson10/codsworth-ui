const {test} = require('node:test');
const assert = require('node:assert/strict');
const {CodsworthAnimator} = require('../glyphs/animator.js');
const catalog = require('../glyphs/animations/manifest.json');
function fixture(reducedMotion = false, procedural = false) {
  let time = 0, next = 1;
  const scheduled = new Map(), draws = [], images = [];
  const poses = [];
  const canvas = {getContext: () => ({clearRect() {}, drawImage(...args) { draws.push(args); },
    createImageData(w,h) {return {data:new Uint8ClampedArray(w*h*4)};}, putImageData() {} })};
  const animator = new CodsworthAnimator(canvas, catalog, {
    reducedMotion, now: () => time,
    renderer: procedural ? {render(pose) {poses.push(structuredClone(pose)); return new Uint8ClampedArray(catalog.width*catalog.height*4);}} : undefined,
    requestFrame(fn) { const id = next++; scheduled.set(id, fn); return id; },
    cancelFrame(id) { scheduled.delete(id); },
    imageFactory() { const image = {}; images.push(image); return image; },
  });
  return {animator, scheduled, images, draws, poses, setTime(t) { time = t; }};
}
test('expression survives talking, pause and end; interrupted activity resumes', () => {
  const {animator} = fixture(true);
  const audio = new EventTarget(); audio.paused = true;
  animator.setExpression('skeptical').setActivity('listening');
  const unbind = animator.bindAudio(audio);
  audio.dispatchEvent(new Event('play'));
  audio.dispatchEvent(new Event('playing'));
  assert.equal(animator.key, 'skeptical-talking');
  audio.dispatchEvent(new Event('pause'));
  assert.equal(animator.key, 'skeptical-listening');
  audio.dispatchEvent(new Event('play'));
  audio.dispatchEvent(new Event('ended'));
  assert.equal(animator.key, 'skeptical-listening');
  unbind();
  audio.dispatchEvent(new Event('play'));
  assert.equal(animator.activity, 'listening');
  animator.destroy();
});
test('late sheet loads cannot paint a previously selected expression', () => {
  const f = fixture(true);
  f.animator.setExpression('annoyed');
  f.images[0].onload();
  assert.equal(f.draws.length, 0);
  f.images[1].onload();
  assert.equal(f.draws.length, 1);
  assert.equal(f.draws[0][0], f.images[1]);
});
test('state changes retain phase; reduced motion uses still frame and cancels the loop', () => {
  const f = fixture();
  f.images[0].onload();
  f.setTime(240);
  f.animator.setExpression('pleased');
  f.images[1].onload();
  assert.equal(f.draws.at(-1)[1], catalog.width * 3);
  f.animator.setReducedMotion(true);
  assert.equal(f.scheduled.size, 0);
  assert.equal(f.draws.at(-1)[1], 0);
  f.animator.setReducedMotion(false);
  assert.equal(f.scheduled.size, 1);
  f.animator.destroy();
  assert.equal(f.scheduled.size, 0);
});
test('unknown activity and expression leave the current selection intact', () => {
  const {animator} = fixture(true);
  assert.throws(() => animator.setExpression('missing'), RangeError);
  assert.throws(() => animator.setActivity('missing'), RangeError);
  assert.equal(animator.key, 'neutral-idle');
});
test('one-shot action completes once, emits contact cues and restores expression', async () => {
  const f = fixture();
  const cues = [];
  f.animator.onActionFrame = cue => cues.push(cue);
  f.animator.setExpression('skeptical');
  const done = f.animator.playAction('open-panel');
  f.images.at(-1).onload();
  f.setTime(1800); f.animator._draw(1800);
  assert.equal(f.animator.key, 'action-open-panel');
  assert.ok(cues.some(cue => cue.contact));
  f.setTime(4000); f.animator._draw(4000);
  assert.deepEqual(await done, {cancelled: false});
  assert.equal(f.animator.key, 'skeptical-idle');
  assert.equal(cues.at(-1).progress, 1);
  f.animator.destroy();
});
test('cancellation prevents a late action load from replaying the scene', async () => {
  const f = fixture(); const cues = [];
  f.animator.onActionFrame = cue => cues.push(cue);
  const done = f.animator.playAction('inspect-error');
  const pending = f.images.at(-1);
  f.animator.cancelAction(); pending.onload();
  assert.deepEqual(await done, {cancelled: true});
  assert.equal(cues.length, 0);
  f.animator.destroy();
});
test('reduced-motion actions commit their final state without an animation loop', async () => {
  const f = fixture(true); const cues = [];
  f.animator.onActionFrame = cue => cues.push(cue);
  const done = f.animator.playAction('present-result');
  f.images.at(-1).onload();
  assert.deepEqual(await done, {cancelled: false});
  assert.equal(cues.length, 1);
  assert.equal(cues[0].progress, 1);
  assert.equal(f.scheduled.size, 0);
});

test('failed action load settles cancellation and permits retry', async () => {
  const f = fixture(true); const errors = [];
  f.animator.onError = error => errors.push(error.message);
  const done = f.animator.playAction('open-panel');
  f.images.at(-1).onerror();
  assert.deepEqual(await done, {cancelled: true});
  assert.equal(f.animator.key, 'neutral-idle');
  assert.equal(errors.length, 1);
  const retry = f.animator.playAction('open-panel');
  f.images.at(-1).onload();
  assert.deepEqual(await retry, {cancelled: false});
});

test('pleased hops once on entry, annoyed eases down and exits back to baseline', () => {
 const f=fixture(); f.images[0].onload();
 f.animator.setExpression('pleased'); f.images.at(-1).onload();
 f.setTime(260); f.animator._draw(260);
 assert.equal(f.animator.offsetY,-2);
 f.setTime(600); f.animator._draw(600);
 assert.equal(f.animator.offsetY,0);
 f.animator.setExpression('annoyed'); f.images.at(-1).onload();
 f.setTime(860); f.animator._draw(860);
 assert.equal(f.animator.offsetY,2);
 f.setTime(1200); f.animator._draw(1200);
 assert.equal(f.animator.offsetY,4);
 f.animator.setExpression('neutral');
 f.setTime(1800); f.animator._draw(1800);
 assert.equal(f.animator.offsetY,0);
 f.animator.destroy();
});
test('real intermediate poses retarget continuously and reduced motion settles immediately', () => {
 const f=fixture(false,true);
 const start=structuredClone(f.animator.lastPose);
 f.animator.setExpression('annoyed');
 assert.deepEqual(f.animator.lastPose,start);
 f.setTime(320); f.animator._draw(320);
 assert.ok(f.animator.lastPose.glare>0 && f.animator.lastPose.glare<1);
 assert.ok(f.animator.lastPose.grip>0 && f.animator.lastPose.grip<.85);
 const intermediate=structuredClone(f.animator.lastPose);
 f.animator.setExpression('pleased');
 assert.deepEqual(f.animator.lastPose,intermediate);
 f.setTime(480);f.animator._draw(480);
 assert.ok(f.animator.lastPose.glare<intermediate.glare);
 f.animator.setReducedMotion(true);
 assert.equal(f.animator.transition,null);
 assert.equal(f.animator.lastPose.glare,0);
 assert.equal(f.animator.lastPose.reach,2);
 f.animator.destroy();
});
test('activity transitions move the thinking claw through intermediate joint positions', () => {
 const f=fixture(false,true);
 f.animator.setActivity('thinking');
 f.setTime(320);f.animator._draw(320);
 assert.ok(f.animator.lastPose.lift>0 && f.animator.lastPose.lift<18);
 f.setTime(640);f.animator._draw(640);
 assert.equal(f.animator.transition,null);
 assert.ok(f.animator.lastPose.lift>=18);
 f.animator.destroy();
});

test('reselecting a failed expression retries its image without replaying a loaded gesture', () => {
 const f=fixture(true); f.animator.setExpression('pleased');
 f.images.at(-1).onerror(); const count=f.images.length;
 f.animator.setExpression('pleased');
 assert.equal(f.images.length,count+1);
 f.images.at(-1).onload();
 f.animator.setExpression('pleased');
 assert.equal(f.images.length,count+1);
 f.animator.destroy();
});
