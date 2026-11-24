#!/usr/bin/env python3
"""Capture the actual terminal renderer with disposable, fictional demo state.

Requires Pillow and ffmpeg. No server, model, audio or saved user state is read.
Run from any directory: python scripts/capture_terminal.py [--still-only].
The adapter preserves Terminal.draw's cell positions, xterm colors and real PNG
glyph frames. Only the terminal emulator's rasterization is supplied here.
"""
from __future__ import annotations

import argparse
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
import terminal
from glyph_terminal import frame_png
from keyboard import Key
from task_store import TaskStore

COLS, ROWS, CELL_W, CELL_H = 120, 34, 12, 24
BACKGROUND = (16, 18, 21)
FPS, SECONDS = 12, 10
PAIRS = {1:(252,-1),2:(244,-1),3:(240,-1),4:(114,-1),5:(222,-1),
         6:(151,234),7:(114,234),8:(203,-1),9:(243,234),10:(203,234),
         11:(255,-1),12:(180,234),13:(252,234),14:(255,234),15:(222,234)}


def font_path():
    supplied = os.environ.get("CODS_CAPTURE_FONT")
    candidates = [supplied, "/System/Library/Fonts/Menlo.ttc",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]
    return next((p for p in candidates if p and Path(p).is_file()), None)


def xterm(index):
    if index < 0:
        return BACKGROUND
    base = [(0,0,0),(205,0,0),(0,205,0),(205,205,0),(0,0,238),(205,0,205),(0,205,205),(229,229,229),
            (127,127,127),(255,0,0),(0,255,0),(255,255,0),(92,92,255),(255,0,255),(0,255,255),(255,255,255)]
    if index < 16:
        return base[index]
    if index >= 232:
        return (8 + 10*(index-232),)*3
    cube = (0,95,135,175,215,255)
    value = index - 16
    return cube[value//36], cube[(value//6)%6], cube[value%6]


class CaptureScreen:
    """The small curses surface used by Terminal.draw; no alternate UI layout."""
    def __init__(self):
        chosen = font_path()
        if not chosen:
            raise RuntimeError("Set CODS_CAPTURE_FONT to a monospaced TrueType font")
        self.regular = ImageFont.truetype(chosen, 20)
        try:
            self.bold = ImageFont.truetype(chosen, 20, index=1) if chosen.endswith('.ttc') else self.regular
        except OSError:
            self.bold = self.regular
        self.erase()

    def getmaxyx(self):
        return ROWS, COLS

    def erase(self):
        self.cells = [[(' ', 1 << 8) for _ in range(COLS)] for _ in range(ROWS)]

    def refresh(self):
        pass

    def move(self, y, x):
        self.cursor = (y, x)

    def addnstr(self, y, x, text, n, attr):
        for character in text[:n]:
            span = 0 if unicodedata.combining(character) else (2 if unicodedata.east_asian_width(character) in ('W','F') else 1)
            if span and 0 <= y < ROWS and 0 <= x < COLS:
                self.cells[y][x] = (character, attr)
                for extra in range(1, span):
                    if x + extra < COLS:
                        self.cells[y][x+extra] = ('', attr)
            x += span

    def image(self, app):
        bitmap = Image.new('RGB', (COLS*CELL_W, ROWS*CELL_H), BACKGROUND)
        draw = ImageDraw.Draw(bitmap)
        for y, row in enumerate(self.cells):
            for x, (character, attr) in enumerate(row):
                foreground, background = PAIRS.get((attr >> 8) & 255, PAIRS[1])
                foreground, background = xterm(foreground), xterm(background)
                if attr & terminal.curses.A_REVERSE:
                    foreground, background = background, foreground
                if background != BACKGROUND:
                    draw.rectangle((x*CELL_W,y*CELL_H,(x+1)*CELL_W-1,(y+1)*CELL_H-1), fill=background)
                if character.strip():
                    draw.text((x*CELL_W,y*CELL_H+19),character,
                              font=self.bold if attr & terminal.curses.A_BOLD else self.regular,
                              fill=foreground,anchor='ls')
                if attr & terminal.curses.A_UNDERLINE:
                    draw.line((x*CELL_W,y*CELL_H+22,(x+1)*CELL_W,y*CELL_H+22),fill=foreground)
        glyph = getattr(app, '_glyph_key', None)
        if glyph:
            expression, activity, frame, columns, rows, top, left = glyph
            sprite = Image.open(io.BytesIO(frame_png(expression,activity,frame))).convert('RGBA')
            sprite = sprite.resize((columns*CELL_W,rows*CELL_H),Image.Resampling.NEAREST)
            bitmap.paste(sprite,(left*CELL_W,top*CELL_H),sprite)
        return bitmap


def demo_app(state_path):
    app = terminal.Terminal('http://127.0.0.1:1',muted=True,state_path=state_path)
    # A visible fictional label, independent of the disposable directory on disk.
    app.cwd = Path('/demo/orbital-weather')
    app.state.title = 'UI fixture'
    app.settings.color = 'green'
    app.key_protocol = True
    app.glyph_graphics = True
    app.tray_open, app.tray_view = True, 'tasks'
    app.expression = 'pleased'
    app.status = 'Scripted UI fixture · no model connected'
    app.transcript = [
        ('You','Keep the interface work separate from the data mock.'),
        ('Codsworth','Separate workstreams keep each conversation and draft together. '
         'Use the tray to see them without leaving this conversation.'),
        ('You','Help me prepare the orbital-weather demo for review.'),
        ('Codsworth','A tidy plan, sir. Three small tasks and one clear handoff.\n\n'
         '**Interface** — check focus order and keyboard controls.\n'
         '**Documentation** — explain the local setup and demo data.\n'
         '**Walkthrough** — show the task tray and a separate workstream.'),
        ('You','Keep the checklist visible while I review the release notes.'),
        ('Codsworth','The task tray keeps the remaining work beside the conversation. '
         'Switching trays leaves your draft and conversation target intact.\n\n'
         'This is a scripted UI fixture. No model, backend or live project is connected.'),
    ]
    app.state.task_store = TaskStore([
        {'id':'d0c50001000000000000000000000001','title':'Write setup notes','status':'open','priority':'high'},
        {'id':'d0c50002000000000000000000000002','title':'Check keyboard flow','status':'open','priority':'normal'},
        {'id':'d0c50003000000000000000000000003','title':'Record local demo','status':'open','priority':'normal'},
    ])
    app.workstreams.records = [
        {'id':'cafe0001000000000000000000000001','title':'Release notes','status':'idle','session_id':'','transcript':[],
         'draft':'','summary':'Fictional demo workstream','unread':False},
        {'id':'cafe0002000000000000000000000002','title':'Keyboard review','status':'idle','session_id':'','transcript':[],
         'draft':'','summary':'Fictional demo workstream','unread':False},
    ]
    return app


def render(app, screen, moment):
    # Force a complete capture of the real draw function at each deterministic time.
    app._draw_key = None
    with patch.object(terminal.curses,'color_pair',side_effect=lambda n:n << 8), \
         patch.object(terminal.curses,'COLORS',256,create=True), \
         patch.object(terminal.curses,'init_pair',side_effect=lambda n,fg,bg:PAIRS.update({n:(fg,bg)})), \
         patch.object(terminal.sys,'stdout',new=io.StringIO()), \
         patch.object(terminal.time,'monotonic',return_value=moment), \
         patch.object(terminal,'kitty_frame',return_value=''):
        app.draw(screen)
    return screen.image(app)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--still-only',action='store_true')
    args=parser.parse_args()
    media=ROOT/'media'; media.mkdir(exist_ok=True)
    screen=CaptureScreen()
    previous=Path.cwd()
    with tempfile.TemporaryDirectory(prefix='codsworth-capture-') as disposable:
        os.chdir(disposable)
        try:
            app=demo_app(Path(disposable)/'state.json')
            app.input='Keep the release checklist in view.'
            render(app,screen,100.0).save(media/'terminal.png',optimize=True)
            if args.still_only:
                return
            executable=shutil.which('ffmpeg')
            if not executable:
                raise RuntimeError('ffmpeg is required for video capture')
            command=[executable,'-y','-hide_banner','-loglevel','error','-f','rawvideo','-pix_fmt','rgb24',
                     '-s',f'{COLS*CELL_W}x{ROWS*CELL_H}','-r',str(FPS),'-i','pipe:0','-an','-c:v','libx264',
                     '-preset','slow','-crf','22','-pix_fmt','yuv420p','-movflags','+faststart',str(media/'terminal-demo.mp4')]
            process=subprocess.Popen(command,stdin=subprocess.PIPE)
            for frame in range(FPS*SECONDS):
                if frame == FPS*3:
                    app.handle_key(Key('4',modifiers=2,code=52))  # actual Alt+4 streams shortcut
                if frame == FPS*5:
                    app.handle_key(Key('5',modifiers=2,code=53))  # actual Alt+5 tasks shortcut
                    app.input=''
                if FPS*5 <= frame < FPS*8:
                    draft='Keep the release checklist in view.'
                    index=frame-FPS*5
                    if index < len(draft):
                        app.handle_key(Key(draft[index],code=ord(draft[index])))
                image=render(app,screen,100.0+frame/FPS)
                process.stdin.write(image.tobytes())
            process.stdin.close()
            if process.wait():
                raise RuntimeError('ffmpeg failed to encode the terminal capture')
            subprocess.run([executable,'-y','-hide_banner','-loglevel','error','-i',str(media/'terminal-demo.mp4'),
                '-filter_complex','[0:v]fps=6,scale=864:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=bayer:bayer_scale=4',
                '-loop','0',str(media/'terminal-demo.gif')],check=True)
        finally:
            os.chdir(previous)


if __name__=='__main__':
    main()
