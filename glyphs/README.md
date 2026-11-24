# Codsworth character and interaction system

The character uses a 96×112 grid with five expressions and four activities. Its browser and terminal renderers share the same geometry, palette, pose parameters, and exported clips.

- [Expressions](preview.html): choose a mood or activity, pause motion, or use a local audio file.
- [Conversation gestures](conversation.html): scripted text replies and optional local audio with explicit expression cues.
- [Screen interactions](actions.html): the character opens a panel, presents a result, or inspects an error.

No browser demo calls a model or uploads audio. The source audio corpus and trained voice adapters are separate from this UI release.

```sh
python3 glyphs/codsworth.py --ascii
python3 -m venv .venv
.venv/bin/pip install -r glyphs/requirements.txt
mkdir -p outputs/glyph-preview
.venv/bin/python glyphs/codsworth.py --out outputs/glyph-preview --px 3
```

For embedding, load `animations/manifest.js`, `renderer.js`, and `animator.js`, then instantiate `CodsworthAnimator` with a canvas and `CODSWORTH_ANIMATIONS`. Call `setExpression`, `setActivity`, or `setReducedMotion` explicitly. `interaction.js` coordinates reply generations, interruption, and audio lifecycle. `scenes.js` coordinates screen actions with host DOM elements.
