# Conversation expressions

[Try the conversation demo](conversation.html). [Browse every expression](preview.html).

`CodsworthInteraction` connects semantic reply metadata to the sprite animator. The same expression takes different timing from its medium: speech follows actual playback; terminal output follows when text becomes visible and how long it takes to read. `CodsworthAnimator` owns the visual transition and entry gestures. Repeated playback ticks do not retrigger a pleased hop.

Load `animations/manifest.js`, `renderer.js`, `animator.js`, then `interaction.js` as plain scripts, or use the CommonJS export for tests. Do not also call the animator's older `bindAudio()` on the same audio element: this controller owns the reply lifecycle.

```js
const interaction = new CodsworthInteraction(animator);
const reply = {
  id: 'turn-18-reply',
  generation: 18,
  medium: 'speech', // or 'text'
  expression: 'pleased'
};
interaction.beginReply(reply); // neutral + thinking while preparing output
interaction.bindSpeech(reply, audio, [
  {at: 2.4, expression: 'skeptical'},
  {at: 5.1, expression: 'pleased'}
]);
await audio.play();
```

Speech enters its expression and talking activity on the audio element's `playing` event. A synthesis result or `play()` request alone does not trigger talking. Optional cues use seconds on `audio.currentTime`; seeks recompute the appropriate expression. Browser `timeupdate` provides clause-level timing, not precise word or lip synchronization. Pause or buffering settles to neutral idle, resume restores the expression at the current playback position, and end/error finishes the reply. Bind before playback and use one player/source per reply in production; the audio engine can stream into that source. Never reuse a source for a different utterance while callbacks for its previous decoding operation are outstanding.

```js
const reply = {id: 'turn-19-reply', generation: 19,
               medium: 'text', expression: 'annoyed'};
interaction.beginReply(reply);
// Later, after rendering the reply into the terminal/UI:
interaction.revealReply(reply, {text: renderedText});
// Optional: settle as soon as the application considers the reply complete.
interaction.finishReply(reply);
```

Text has no talking animation. Reveal starts the selected expression, then a readability timer settles to neutral idle. The default is 600 ms plus 240 ms per whitespace-delimited word, clamped to 1.2–8 seconds. `holdMs` can override the estimate within those bounds. For streamed terminal output, call reveal as a meaningful clause is displayed, then again as the visible reply grows; each call replaces the preceding timer. Pass `expression` in reveal options to change mood at a displayed clause boundary;
it persists for subsequent text updates until another explicit cue. Unsupported
expressions are rejected before altering the current pose. Text remains on screen
after the expression settles.

`interrupt({medium: 'speech'})` immediately pauses the owned audio, detaches listeners, clears timers and cancels any screen action, then transitions to neutral listening. Text interruption settles to neutral idle. The application must independently cancel its STT/Qwen/TTS generation and queued audio: this small controller does not own those services or undo tool operations. `destroy()` clears the controller's work and settles the animator, but leaves the animator itself available to its owner.

Reply IDs and nonnegative integer generations come from the application coordinator. Every new reply must have a strictly newer generation. Late reveal/finish/bind requests for older replies return `false`; invalid metadata throws. Already queued audio callbacks and text timers are guarded by the current reply and reveal instance. Supported expressions come from the animator catalog. Clause cues must be ordered, nonnegative timestamps with supported expressions. No sentiment keywords are guessed from user text; the trusted application validates the agent's proposed semantic expression before passing it here.

The demo uses curated text and the archived parking recording to show playback behavior. Its expression selector is an explicit demonstration cue, not an emotion analysis of that recording. Reduced motion is delegated to the animator, preserving state changes without animated hops.

Run unit checks with `node --test tests/interaction.test.cjs`. The optional Playwright check is `node tests/conversation.browser.cjs` with the same `NODE_PATH` and `CODS_BROWSER_PATH` setup as the other browser checks.
