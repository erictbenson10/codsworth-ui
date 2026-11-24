/* Semantic reply lifecycle: audio playback and text visibility drive the same animator. */
(function (root) {
  'use strict';
  class CodsworthInteraction {
    constructor(animator, options = {}) {
      this.animator = animator;
      this.schedule = options.setTimeout || ((fn, ms) => setTimeout(fn, ms));
      this.clear = options.clearTimeout || (id => clearTimeout(id));
      this.current = null;
      this.generation = -1;
      this.destroyed = false;
    }
    _validate(reply) {
      if (!reply || typeof reply.id !== 'string' || !reply.id.trim() ||
          !Number.isSafeInteger(reply.generation) || reply.generation < 0 ||
          !['speech', 'text'].includes(reply.medium) ||
          !this.animator.catalog.expressions.includes(reply.expression)) {
        throw new TypeError('Reply requires id, nonnegative generation, speech/text medium and a catalog expression');
      }
    }
    _matches(reply) {
      return !this.destroyed && this.current && this.current.reply.id === reply.id &&
        this.current.reply.generation === reply.generation && this.current.reply.medium === reply.medium;
    }
    _pose(expression, activity) {
      if (this.animator.expression !== expression) this.animator.setExpression(expression);
      if (this.animator.activity !== activity) this.animator.setActivity(activity);
    }
    _release() {
      const current = this.current;
      this.current = null; // Invalidate even already queued callbacks before pausing audio.
      if (!current) return;
      if (current.timer !== null) this.clear(current.timer);
      current.unbind?.();
      current.audio?.pause();
    }
    beginReply(reply) {
      this._validate(reply);
      if (this.destroyed) return false;
      if (this._matches(reply)) return true;
      if (reply.generation <= this.generation) return false;
      this._release();
      this.animator.cancelAction();
      this.generation = reply.generation;
      this.current = {reply: {...reply}, timer: null, audio: null, unbind: null};
      this._pose('neutral', 'thinking');
      return true;
    }
    bindSpeech(reply, audio, cues = []) {
      this._validate(reply);
      if (reply.medium !== 'speech') throw new TypeError('Speech binding requires speech medium');
      if (!Array.isArray(cues) || cues.some((cue, i) => !cue || !Number.isFinite(cue.at) || cue.at < 0 ||
          (i > 0 && cue.at < cues[i - 1].at) || !this.animator.catalog.expressions.includes(cue.expression))) {
        throw new TypeError('Speech cues require sorted nonnegative seconds and catalog expressions');
      }
      if (!this._matches(reply)) return false;
      const current = this.current;
      if (current.audio) throw new Error('A reply already has an audio binding');
      const timeline = cues.map(cue => ({...cue}));
      const valid = () => this.current === current && !this.destroyed;
      let audible = false;
      const expressionAtTime = () => {
        let expression = current.reply.expression;
        for (const cue of timeline) {
          if (cue.at > audio.currentTime) break;
          expression = cue.expression;
        }
        return expression;
      };
      const playing = () => {
        if (!valid()) return;
        audible = true;
        this._pose(expressionAtTime(), 'talking');
      };
      const timeupdate = () => { if (valid() && audible) this._pose(expressionAtTime(), 'talking'); };
      const pause = () => {
        if (!valid()) return;
        audible = false;
        this._pose('neutral', 'idle');
      };
      const finish = () => { if (valid()) this.finishReply(current.reply); };
      const events = {playing, timeupdate, seeked: timeupdate, pause, waiting: pause, ended: finish, error: finish};
      for (const [event, handler] of Object.entries(events)) audio.addEventListener(event, handler);
      current.audio = audio;
      current.unbind = () => {
        for (const [event, handler] of Object.entries(events)) audio.removeEventListener(event, handler);
      };
      // Bind before playback. A play request alone is not evidence of audible output.
      return true;
    }
    revealReply(reply, options = {}) {
      this._validate(reply);
      if (reply.medium !== 'text') throw new TypeError('Text reveal requires text medium');
      if (!this._matches(reply)) return false;
      if (options.holdMs !== undefined && (!Number.isFinite(options.holdMs) || options.holdMs < 0)) {
        throw new TypeError('holdMs must be finite and nonnegative');
      }
      if (options.expression !== undefined && !this.animator.catalog.expressions.includes(options.expression)) {
        throw new TypeError('Text cue requires a catalog expression');
      }
      const current = this.current;
      if (current.timer !== null) this.clear(current.timer);
      current.textExpression = options.expression ?? current.textExpression ?? current.reply.expression;
      this._pose(current.textExpression, 'idle');
      const words = String(options.text || '').trim().split(/\s+/).filter(Boolean).length;
      const holdMs = Math.min(8000, Math.max(1200, options.holdMs ?? (600 + words * 240)));
      const revealToken = {};
      current.revealToken = revealToken;
      current.timer = this.schedule(() => {
        if (this.current === current && current.revealToken === revealToken && !this.destroyed) this.finishReply(current.reply);
      }, holdMs);
      return true;
    }
    finishReply(reply) {
      this._validate(reply);
      if (!this._matches(reply)) return false;
      this._release();
      this._pose('neutral', 'idle');
      return true;
    }
    interrupt(options = {}) {
      const medium = options.medium || this.current?.reply.medium || 'text';
      if (!['speech', 'text'].includes(medium)) throw new TypeError('Unknown interaction medium');
      if (this.destroyed) return false;
      this._release();
      this.animator.cancelAction();
      this._pose('neutral', medium === 'speech' ? 'listening' : 'idle');
      return true;
    }
    destroy() {
      if (this.destroyed) return;
      this.interrupt({medium: 'text'});
      this.destroyed = true;
    }
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {CodsworthInteraction};
  else root.CodsworthInteraction = CodsworthInteraction;
})(globalThis);
