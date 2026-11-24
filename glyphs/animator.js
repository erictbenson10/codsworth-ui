/* Browser sprite controller. Plain-script and CommonJS entry points. */
(function (root) {
  'use strict';
  class CodsworthAnimator {
    constructor(canvas, catalog, options = {}) {
      this.canvas = canvas;
      this.context = canvas.getContext('2d');
      if (!this.context) throw new Error('A 2D canvas is required');
      this.catalog = catalog;
      this.baseUrl = options.baseUrl || 'animations/';
      this.makeImage = options.imageFactory || (() => new Image());
      this.request = options.requestFrame || (fn => requestAnimationFrame(fn));
      this.cancel = options.cancelFrame || (id => cancelAnimationFrame(id));
      this.now = options.now || (() => performance.now());
      this.onChange = options.onChange || (() => {});
      this.onError = options.onError || (() => {});
      this.onActionFrame = options.onActionFrame || (() => {});
      this.action = null;
      this.transition = null;
      this.renderer = options.renderer || root.CodsworthRenderer;
      this.lastPose = null;
      this.lastRenderTime = -Infinity;
      this.motion = null;
      this.offsetY = 0;
      this.activity = 'idle';
      this.expression = 'neutral';
      this.epoch = this.now();
      this.images = new Map();
      this.bindings = new Set();
      this.reducedMotion = Boolean(options.reducedMotion);
      this.destroyed = false;
      this.frameRequest = null;
      canvas.width = catalog.width;
      canvas.height = catalog.height;
      this.context.imageSmoothingEnabled = false;
      this._load();
      this._tick(this.epoch);
    }
    get key() { return this.action ? `action-${this.action.name}` : `${this.expression}-${this.activity}`; }
    playAction(name) {
      if (!this.catalog.actions?.includes(name)) throw new RangeError(`Unknown action: ${name}`);
      if (this.destroyed) throw new Error('Animator is destroyed');
      this.cancelAction();
      this.transition = null; this.motion = null; this.offsetY = 0;
      return new Promise(resolve => {
        this.action = {name, started: null, resolve};
        this._changed();
      });
    }
    cancelAction() {
      if (!this.action) return false;
      const action = this.action;
      this.action = null;
      action.resolve({cancelled: true});
      this._changed();
      return true;
    }
    setActivity(activity) {
      if (!this.catalog.activities.includes(activity)) throw new RangeError(`Unknown activity: ${activity}`);
      if (this.activity === activity) {
        if (!this.images.has(this.key)) this._changed();
        return this;
      }
      this._beginTransition();
      this.activity = activity;
      this._changed();
      return this;
    }
    setExpression(expression) {
      if (!this.catalog.expressions.includes(expression)) throw new RangeError(`Unknown expression: ${expression}`);
      if (this.expression === expression) {
        if (!this.images.has(this.key)) this._changed();
        return this;
      }
      this._beginTransition();
      this.motion = this.reducedMotion ? null : {started: null, from: this.offsetY,
                     to: expression === 'annoyed' ? 4 : 0, hop: expression === 'pleased'};
      this.expression = expression;
      this._changed();
      return this;
    }
    _beginTransition() {
      if (this.destroyed || this.action || this.reducedMotion || !this.lastPose) return;
      this.transition = {started: this.now(), from: structuredClone(this.lastPose)};
    }
    _mix(from, to, amount) {
      if (Array.isArray(to)) return to.map((value, i) => this._mix(from[i], value, amount));
      if (to !== null && typeof to === 'object') {
        return Object.fromEntries(Object.keys(to).map(key => [key, this._mix(from[key], to[key], amount)]));
      }
      return Number(from) + (Number(to) - Number(from)) * amount;
    }
    _drawPose(time, clip) {
      const position = this.reducedMotion ? 0 : Math.max(0, time - this.epoch) / this.catalog.frameMs % clip.poses.length;
      const index = Math.floor(position);
      const next = structuredClone(clip.poses[(index + 1) % clip.poses.length]);
      // The twelve-tooth saw finishes four identical tooth periods per loop.
      if (index === clip.poses.length - 1) next.turn += Math.PI * 2 / 3;
      let target = this._mix(clip.poses[index], next, position - index);
      const lower = this.expression === 'annoyed' ? 4 : 0;
      target.bob += lower;
      if (this.motion && !this.reducedMotion) {
        if (this.motion.started === null) this.motion.started = time;
        const t = Math.min(1, Math.max(0, (time - this.motion.started) / 720));
        if (this.motion.hop) target.bob -= 2 * Math.sin(Math.PI * t) ** 2;
        if (t === 1) this.motion = null;
      }
      if (this.transition && !this.reducedMotion) {
        const t = Math.min(1, Math.max(0, (time - this.transition.started) / 640));
        const ease = t*t*t*(t*(t*6-15)+10);
        // Nearest equivalent tooth angle prevents a backwards saw sweep at wrap.
        const period = Math.PI / 6;
        const from = {...this.transition.from};
        from.turn += Math.round((target.turn - from.turn) / period) * period;
        target = this._mix(from, target, ease);
        if (t === 1) this.transition = null;
      } else this.transition = null;
      this.lastPose = target;
      this.lastRenderTime = time;
      this.offsetY = lower;
      const picture = this.context.createImageData(this.catalog.width, this.catalog.height);
      picture.data.set(this.renderer.render(target));
      this.context.putImageData(picture, 0, 0);
    }
    _changed() {
      if (this.destroyed) return;
      this._load();
      this._draw(this.now());
      this.onChange({activity: this.activity, expression: this.expression});
    }
    _load() {
      const key = this.key;
      if (this.images.has(key)) return;
      if (this.renderer && this.catalog.clips[key].poses) {
        this.images.set(key, {ready: true});
        return;
      }
      const entry = {image: this.makeImage(), ready: false};
      this.images.set(key, entry);
      entry.image.onload = () => {
        entry.ready = true;
        if (!this.destroyed && this.key === key) this._draw(this.now());
      };
      entry.image.onerror = () => {
        this.images.delete(key); // Allow an explicit retry after a transient load failure.
        if (!this.destroyed && this.key === key) {
          this.cancelAction();
          this.onError(new Error(`Could not load animation: ${key}`));
        }
      };
      entry.image.src = this.baseUrl + this.catalog.clips[key].src;
    }
    _draw(time) {
      const entry = this.images.get(this.key);
      if (!entry || !entry.ready || this.destroyed) return;
      const clip = this.catalog.clips[this.key];
      if (this.renderer && clip.poses && !this.action) {
        this._drawPose(time, clip);
        return;
      }
      let frame;
      let complete = false;
      if (this.action) {
        if (this.action.started === null) this.action.started = time;
        const duration = (clip.frames - 1) * this.catalog.frameMs;
        const progress = this.reducedMotion ? 1 : Math.min(1, Math.max(0, (time - this.action.started) / duration));
        const position = progress * (clip.frames - 1);
        frame = Math.floor(position);
        const first = clip.cues[frame];
        const second = clip.cues[Math.min(frame + 1, clip.frames - 1)];
        const fraction = position - frame;
        const grip = first.grip.map((value, i) => value + fraction * (second.grip[i] - value));
        this.onActionFrame({name: this.action.name, progress, phase: first.phase,
                            contact: first.contact, grip, reducedMotion: this.reducedMotion});
        complete = progress === 1;
      } else {
        frame = this.reducedMotion ? 0 : Math.floor(Math.max(0, time - this.epoch) / this.catalog.frameMs) % clip.frames;
      }
      const {width, height} = this.catalog;
      this.context.clearRect(0, 0, width, height);
      let offset = this.action ? 0 : this.expression === 'annoyed' ? 4 : 0;
      if (this.motion && !this.action && !this.reducedMotion) {
        if (this.motion.started === null) this.motion.started = time;
        const t = Math.min(1, Math.max(0, (time - this.motion.started) / 520));
        const ease = t * t * (3 - 2 * t);
        offset = this.motion.from + (this.motion.to - this.motion.from) * ease;
        if (this.motion.hop) offset -= 2 * Math.sin(Math.PI * t) ** 2;
        if (t === 1) { offset = this.motion.to; this.motion = null; }
      }
      this.offsetY = offset;
      this.context.drawImage(entry.image, frame * width, 0, width, height,
                             0, Math.round(offset), width, height);
      if (complete && this.action) {
        const action = this.action;
        this.action = null;
        action.resolve({cancelled: false});
        this._changed();
      }
    }

    _tick(time) {
      this.frameRequest = null;
      if (this.destroyed) return;
      if (this.reducedMotion || this.action || !this.renderer || time - this.lastRenderTime >= 1000 / 30) this._draw(time);
      if (!this.reducedMotion) this.frameRequest = this.request(t => this._tick(t));
    }
    setReducedMotion(reduced) {
      this.reducedMotion = Boolean(reduced);
      if (this.reducedMotion) this.motion = null;
      if (this.frameRequest !== null) this.cancel(this.frameRequest);
      this.frameRequest = null;
      if (!this.destroyed) this._tick(this.now());
    }
    bindAudio(audio) {
      let speaking = false;
      let previous = this.activity;
      const start = () => {
        if (!speaking) previous = this.activity;
        speaking = true;
        this.setActivity('talking');
      };
      const stop = () => {
        if (!speaking) return;
        speaking = false;
        this.setActivity(previous);
      };
      const events = {play: start, playing: start, pause: stop, ended: stop, error: stop};
      for (const [event, handler] of Object.entries(events)) audio.addEventListener(event, handler);
      const unbind = () => {
        for (const [event, handler] of Object.entries(events)) audio.removeEventListener(event, handler);
        stop();
        this.bindings.delete(unbind);
      };
      this.bindings.add(unbind);
      if (!audio.paused && !audio.ended) start();
      return unbind;
    }
    destroy() {
      this.cancelAction();
      for (const unbind of this.bindings) unbind();
      this.destroyed = true;
      if (this.frameRequest !== null) this.cancel(this.frameRequest);
      this.frameRequest = null;
    }
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {CodsworthAnimator};
  else root.CodsworthAnimator = CodsworthAnimator;
})(globalThis);
