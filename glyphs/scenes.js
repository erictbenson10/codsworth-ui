/* Choreography for real DOM panels in the local interaction playground. */
(function () {
  const world = document.querySelector('#world');
  const viewport = document.querySelector('#viewport');
  const actor = document.querySelector('#actor');
  const panel = document.querySelector('#panel');
  const result = document.querySelector('#result');
  const error = document.querySelector('#error-card');
  const status = document.querySelector('#scene-status');
  const still = document.querySelector('#scene-still');
  const buttons = [...document.querySelectorAll('[data-action]')];
  const preference = matchMedia('(prefers-reduced-motion: reduce)');
  still.checked = preference.matches;
  let generation = 0;
  let current = null;
  const ease = t => { t = Math.max(0, Math.min(1, t)); return t * t * (3 - 2 * t); };
  const blend = (a, b, t) => a.map((v, i) => v + (b[i] - v) * t);
  const place = (element, x, y) => { element.style.transform = `translate(${x}px, ${y}px)`; };
  const origin = (target, grip) => [target[0] - grip[0] * 2, target[1] - grip[1] * 2];
  const animator = new CodsworthAnimator(actor, CODSWORTH_ANIMATIONS, {
    reducedMotion: still.checked,
    onActionFrame(cue) {
      if (cue.name !== current) return;
      const {progress: t, grip} = cue;
      const approach = ease(t / .3), move = ease((t - .37) / .41), settle = ease((t - .78) / .22);
      let target, finalOrigin;
      if (cue.name === 'open-panel') {
        const x = 760 - move * 300;
        place(panel, x, 90);
        target = [x, 210];
        finalOrigin = [290, 130];
      } else if (cue.name === 'present-result') {
        const x = 700 - move * 260;
        place(result, x, 122);
        target = [x, 210];
        finalOrigin = [220, 130];
      } else {
        target = [500, 215];
        finalOrigin = [220, 130];
        const peek = Math.sin(move * Math.PI);
        error.style.transform = `translate(500px, 105px) rotate(${-6 * peek}deg)`;
      }
      const touching = origin(target, grip);
      const lift = cue.name === 'present-result' ? 16 : 12;
      const release = origin(target, [84, 35 - 2 + 31 - lift]);
      const position = t < .3 ? blend([60, 130], touching, approach)
        : t < .78 ? touching : blend(release, finalOrigin, settle);
      place(actor, ...position);
      world.dataset.phase = cue.phase;
      world.dataset.contact = String(cue.contact);
      world.dataset.progress = String(t);
    },
    onError(problem) { status.textContent = problem.message; buttons.forEach(button => button.disabled = false); },
  });
  function reset() {
    generation++;
    current = null;
    animator.cancelAction();
    for (const card of [panel, result, error]) card.hidden = true;
    place(actor, 60, 130);
    world.dataset.phase = 'idle'; world.dataset.contact = 'false'; world.dataset.progress = '0';
    status.textContent = 'Choose an interaction.';
    buttons.forEach(button => button.disabled = false);
  }
  const lines = {
    'open-panel': ['Fetching your particulars…', 'Your particulars, sir.'],
    'present-result': ['Everything accounted for…', 'Presented for your inspection.'],
    'inspect-error': ['Taking a closer look…', 'Ah. There’s your trouble. Ready to retry.'],
  };
  async function run(name) {
    reset();
    const turn = generation;
    current = name;
    buttons.forEach(button => button.disabled = true);
    status.textContent = lines[name][0];
    panel.hidden = name !== 'open-panel'; result.hidden = name !== 'present-result'; error.hidden = name !== 'inspect-error';
    place(panel, 760, 90); place(result, 700, 122); place(error, 500, 105);
    const outcome = await animator.playAction(name);
    if (generation !== turn || outcome.cancelled) return;
    current = null;
    status.textContent = lines[name][1];
    world.dataset.phase = 'complete';
    buttons.forEach(button => button.disabled = false);
  }
  buttons.forEach(button => button.addEventListener('click', () => run(button.dataset.action)));
  document.querySelector('#reset-scene').addEventListener('click', reset);
  still.addEventListener('change', () => animator.setReducedMotion(still.checked));
  preference.addEventListener('change', event => { still.checked = event.matches; animator.setReducedMotion(event.matches); });
  const resize = () => { world.style.transform = `scale(${viewport.clientWidth / 800})`; };
  new ResizeObserver(resize).observe(viewport);
  resize(); reset();
  window.addEventListener('pagehide', () => animator.destroy(), {once: true});
})();
