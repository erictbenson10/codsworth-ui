const test = require('node:test');
const assert = require('node:assert/strict');
const {CodsworthInteraction} = require('../glyphs/interaction.js');
function setup() {
  const animator = {expression: 'neutral', activity: 'idle', cancels: 0, changes: [],
    catalog: {expressions: ['neutral', 'pleased', 'annoyed', 'concerned', 'skeptical']},
    setExpression(value) { this.expression = value; this.changes.push(value); },
    setActivity(value) { this.activity = value; }, cancelAction() { this.cancels++; }};
  const timers = [];
  const interaction = new CodsworthInteraction(animator, {
    setTimeout(fn, ms) { const timer = {fn, ms, cleared: false}; timers.push(timer); return timer; },
    clearTimeout(timer) { timer.cleared = true; }
  });
  return {animator, interaction, timers};
}
function audio() {
  const handlers = new Map();
  return {currentTime: 0, pauses: 0, handlers,
    addEventListener(event, fn) { handlers.set(event, fn); },
    removeEventListener(event, fn) { if (handlers.get(event) === fn) handlers.delete(event); },
    emit(event) { handlers.get(event)?.(); }, pause() { this.pauses++; this.emit('pause'); }};
}
const reply = (generation = 1, medium = 'speech', expression = 'pleased') =>
  ({id: `reply-${generation}`, generation, medium, expression});

test('speech follows actual playback, timed semantic cues and seeks; repeated ticks do not retrigger mood', () => {
  const {animator, interaction} = setup(); const player = audio(); const r = reply();
  interaction.beginReply(r);
  assert.equal(animator.activity, 'thinking');
  interaction.bindSpeech(r, player, [{at: 2, expression: 'annoyed'}, {at: 4, expression: 'pleased'}]);
  player.emit('play');
  assert.equal(animator.activity, 'thinking');
  player.emit('playing');
  assert.equal(animator.expression, 'pleased'); assert.equal(animator.activity, 'talking');
  const changes = animator.changes.length;
  player.emit('timeupdate'); player.emit('timeupdate');
  assert.equal(animator.changes.length, changes);
  player.currentTime = 3; player.emit('timeupdate'); assert.equal(animator.expression, 'annoyed');
  player.currentTime = 0; player.emit('seeked'); assert.equal(animator.expression, 'pleased');
  player.emit('pause'); assert.equal(animator.expression, 'neutral'); assert.equal(animator.activity, 'idle');
  player.currentTime = 3; player.emit('timeupdate'); assert.equal(animator.expression, 'neutral');
  player.emit('playing'); assert.equal(animator.expression, 'annoyed');
  player.emit('waiting'); assert.equal(animator.activity, 'idle');
  player.emit('playing'); player.emit('ended');
  assert.equal(animator.expression, 'neutral'); assert.equal(animator.activity, 'idle');
  assert.equal(player.handlers.size, 0);
});

test('interruption silences owned audio and blocks queued callbacks and obsolete generations', () => {
  const {animator, interaction} = setup(); const player = audio(); const r = reply();
  interaction.beginReply(r); interaction.bindSpeech(r, player); player.emit('playing');
  const queuedPlaying = player.handlers.get('playing'); const queuedEnd = player.handlers.get('ended');
  interaction.interrupt();
  assert.equal(player.pauses, 1); assert.equal(animator.activity, 'listening');
  assert.equal(interaction.beginReply(r), false);
  const next = reply(2, 'text', 'annoyed'); interaction.beginReply(next); interaction.revealReply(next);
  queuedPlaying(); queuedEnd();
  assert.equal(animator.expression, 'annoyed'); assert.equal(animator.activity, 'idle');
  assert.equal(interaction.finishReply(r), false);
});

test('terminal expression belongs to visible output, then explicit finish or readability expiry settles', () => {
  const {animator, interaction, timers} = setup(); const r = reply(1, 'text');
  interaction.beginReply(r); assert.equal(animator.activity, 'thinking');
  assert.equal(animator.expression, 'neutral');
  interaction.revealReply(r, {text: 'Yes, certainly sir.'});
  assert.equal(animator.expression, 'pleased'); assert.equal(animator.activity, 'idle');
  assert.equal(timers[0].ms, 1320);
  interaction.revealReply(r, {holdMs: 99999}); assert.equal(timers[0].cleared, true);
  assert.equal(timers[1].ms, 8000);
  timers[0].fn(); assert.equal(animator.expression, 'pleased');
  interaction.finishReply(r); assert.equal(animator.expression, 'neutral');
  const next = reply(2, 'text', 'concerned'); interaction.beginReply(next);
  interaction.revealReply(next, {holdMs: 0}); assert.equal(timers[2].ms, 1200);
  timers[0].fn(); timers[1].fn(); assert.equal(animator.expression, 'concerned');
  timers[2].fn(); assert.equal(animator.expression, 'neutral'); assert.equal(animator.activity, 'idle');
});

test('superseding a reply clears its timeout and detaches audio errors', () => {
  const {animator, interaction, timers} = setup(); const first = reply(1, 'text');
  interaction.beginReply(first); interaction.revealReply(first);
  const second = reply(2); const player = audio(); interaction.beginReply(second);
  assert.equal(timers[0].cleared, true);
  interaction.bindSpeech(second, player); player.emit('playing'); timers[0].fn();
  assert.equal(animator.activity, 'talking');
  player.emit('error'); assert.equal(animator.activity, 'idle'); assert.equal(animator.expression, 'neutral');
});

test('invalid semantic contracts fail before touching the current reply', () => {
  const {animator, interaction} = setup(); const r = reply(); interaction.beginReply(r);
  assert.throws(() => interaction.beginReply({...r, expression: 'happy'}), TypeError);
  assert.throws(() => interaction.beginReply({...r, generation: 0.5}), TypeError);
  assert.throws(() => interaction.bindSpeech(r, audio(), [{at: 2, expression: 'pleased'}, {at: 1, expression: 'annoyed'}]), TypeError);
  assert.throws(() => interaction.revealReply(r), TypeError);
  assert.equal(animator.activity, 'thinking');
  assert.equal(interaction.bindSpeech(reply(0), audio()), false);
  interaction.bindSpeech(r, audio()); assert.throws(() => interaction.bindSpeech(r, audio()), /already/);
});

test('destroy cancels actions, settles and makes saved audio callbacks and text timers harmless', () => {
  const {animator, interaction, timers} = setup(); const r = reply(1, 'text');
  interaction.beginReply(r); interaction.revealReply(r); interaction.destroy();
  timers[0].fn(); assert.equal(animator.expression, 'neutral'); assert.equal(animator.activity, 'idle');
  assert.equal(interaction.beginReply(reply(2)), false); assert.equal(interaction.interrupt(), false);
  assert.equal(timers[0].cleared, true); assert.ok(animator.cancels >= 2);
});

 test('queued completion uses the copied reply identity when caller metadata mutates', () => {
  const {animator, interaction, timers} = setup(); const first = reply(1, 'text');
  interaction.beginReply(first); interaction.revealReply(first); first.generation = 200;
  timers[0].fn(); assert.equal(animator.expression, 'neutral');
  const second = reply(2); const player = audio();
  interaction.beginReply(second); interaction.bindSpeech(second, player); player.emit('playing');
  second.id = 'mutated'; player.emit('ended'); assert.equal(animator.activity, 'idle');
 });

test('text clause cues change expression when revealed and persist across incremental text', () => {
 const {animator,interaction}=setup(); const r=reply(1,'text','pleased');
 interaction.beginReply(r);
 interaction.revealReply(r,{text:'An intriguing plan.',expression:'skeptical'});
 assert.equal(animator.expression,'skeptical');
 interaction.revealReply(r,{text:'An intriguing plan. Shall I fetch the extinguisher?'});
 assert.equal(animator.expression,'skeptical');
 assert.throws(()=>interaction.revealReply(r,{expression:'missing'}),TypeError);
 assert.equal(animator.expression,'skeptical');
 interaction.finishReply(r);assert.equal(animator.expression,'neutral');
});
