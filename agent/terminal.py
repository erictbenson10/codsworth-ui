#!/usr/bin/env python3
"""Codsworth's native terminal home: streaming chat, bounded panels and speech."""
import argparse
import array
import base64
import collections
import curses
import fcntl
from dataclasses import dataclass, field
from datetime import datetime
import http.client
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import traceback
import tempfile
import time
import unicodedata
import uuid
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from artifact_links import extract_links, valid_url
from command_catalog import matches as command_matches, session_choices, COLOR_OPTIONS, EFFORT_LEVELS
from message_render import render_message, cell_width, highlight_metrics
from turn_progress import work_duration, shimmer_levels
from fleet_view import overview_text
from inbox_view import inbox_text
from workstreams import WorkstreamManager, DisabledWorkstreams
from fleet_watch import FleetWatch, updates_text
from quirks import joke, tea, vault
from diagnostics import collect as collect_diagnostics, format_checks
from keyboard import Decoder, Key, ENABLE as KEYBOARD_ENABLE, DISABLE as KEYBOARD_DISABLE
from microphone import Recording
from conversation_store import ConversationStore
from task_store import TaskStore
from glyph_terminal import fit_cells, kitty_at, kitty_frame, kitty_clear, kitty_clear_previews, kitty_file, PREVIEW_IMAGE_IDS, render_cells
import image_attachments as attachments


def safe_text(value, limit=30000):
    """Model text is data: remove ANSI/OSC escapes and terminal control characters."""
    value = str(value)
    value = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', value)
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
    return ''.join(c for c in value if c in '\n\t' or not unicodedata.category(c).startswith('C'))[:limit]


# Empty-composer suggestions, rotated like Claude Code's example prompts.
SUGGESTIONS = (
    'Ask Codsworth…',
    'Try "help me prioritise today"',
    'Try "/todo draft the proposal"',
    'Try "what is waiting on me in Corner?"',
    'Try "explain this failure in plain English"',
)
SUGGESTION_SECONDS = 6
# The tray on the right (docs/ui-butlers-tray.md 2a). Shut, nothing of it remains
# but a two-column pull tab; open, the width is a property of the contents.
TRAY_COLUMNS = 26
TRAY_WIDE_COLUMNS = 39
TRAY_VIEWS = ('glance', 'fleet', 'inbox', 'streams', 'tasks', 'doctor', 'windows')
TRAY_WIDE_VIEWS = ('fleet', 'inbox', 'windows')
TRAY_TITLES = {'glance': 'At a glance', 'fleet': 'Fleet', 'inbox': 'Corner',
               'streams': 'Streams', 'tasks': 'Tasks', 'doctor': 'Doctor',
               'windows': 'Windows'}
# Below this the tray cannot open at all: the transcript needs the columns (2f).
TRAY_MIN_COLUMNS = 96
TRAY_MIN_WIDE_COLUMNS = 110
# Alt accelerators, registered only under the key protocol; None toggles (2c).
TRAY_ACCELERATORS = {92: None, 49: 'glance', 50: 'fleet', 51: 'inbox',
                     52: 'streams', 53: 'tasks', 54: 'doctor', 55: 'windows'}
# Transcript labels that report a failure; drawn in red rather than Codsworth's green.
ERROR_LABELS = ('Error',)
# A silent upstream turn must not look like an idle terminal: warn, then give up.
STALL_NOTICE = 25
STALL_LIMIT = 180
# Rule 2: sections whose last good read is remembered across a window restart.
LAST_GOOD_SECTIONS = ('issues', 'pulls', 'fleet')
# Rule 4: a one-off failure is red once, then it fades to the plain status row.
NOTICE_FADE = 6
# Warn while a fresh conversation is still cheap, not once the turn already failed.
CONTEXT_WARNING = .8


def waiting_word(text):
    """What Codsworth is doing while you wait.

    A long or multi-part question deserves an answer that says he has taken it
    in, not a generic spinner -- the wait is the same either way, but 'thinking'
    on a three-part question reads like he missed two of them."""
    text = (text or '').strip()
    involved = (len(text) >= 180 or text.count('?') >= 2
                or len([line for line in text.splitlines() if line.strip()]) >= 3)
    return 'taking a look' if involved else 'thinking'


def context_usage(used, limit):
    """(used, limit, fraction) for a reported prompt size, or None when unusable."""
    if type(used) is not int or used < 0:
        return None
    limit = limit if type(limit) is int and limit > 0 else 262144
    return used, limit, min(1.0, used/limit)


def context_label(context):
    used, limit, fraction = context
    # One divisor for both sides, or the printed pair disagrees with its own
    # percentage: 225k/250k reads as 90% beside a correct 87%. 1024 is the
    # right one -- context limits are powers of two, and 262144 is "256k"
    # everywhere it is written down.
    return f'ctx {used/1024:.0f}k/{limit/1024:.0f}k · {int(fraction*100)}%'


def suggestion(slot):
    return SUGGESTIONS[slot % len(SUGGESTIONS)]


def tray_width(view, width):
    """Open width for a view, or 0 when this window is too narrow to open at all."""
    if width >= TRAY_MIN_WIDE_COLUMNS and view in TRAY_WIDE_VIEWS:
        return TRAY_WIDE_COLUMNS
    return TRAY_COLUMNS if width >= TRAY_MIN_COLUMNS else 0


def fleet_questions(snapshot):
    """Open questions, or None when the source could not say. Never a bare empty list."""
    if not isinstance(snapshot, dict) or not snapshot.get('available', {}).get('questions'):
        return None
    questions = snapshot.get('questions')
    return questions if isinstance(questions, list) else None


def badge(mark, count):
    # Two columns is all the shut tab has; a larger count becomes a plain '+'.
    return mark+('+' if count > 9 else str(count))


def tray_badges(data, tasks_open):
    """Pull-tab badges. Red is only for work waiting on Eric; a running attempt is not."""
    badges = []
    fleet = data.get('fleet')
    questions = fleet_questions(fleet)
    if questions:
        badges.append((badge('!', len(questions)), 'alert'))
    if isinstance(fleet, dict):
        running = fleet.get('counts', {}).get('running')
        if isinstance(running, int) and not isinstance(running, bool) and running > 0:
            badges.append((badge('◐', running), 'accent'))
    inbox = data.get('inbox')
    if isinstance(inbox, dict):
        corner = len(inbox.get('issues') or [])+len(inbox.get('pulls') or [])
        if corner:
            badges.append((badge('▲', corner), 'soft'))
    if tasks_open:
        badges.append((badge('□', tasks_open), 'soft'))
    return badges


def open_tasks(task_store):
    return [item for item in task_store.active_records() if item['status'] == 'open']


def stream_rows(records, selected):
    """Home and every workstream, the focused one marked; agrees with composer_target()."""
    rows = [('→ Home' if selected is None else '  Home', 'accent' if selected is None else 'body')]
    for record in records:
        focused = record['id'] == selected
        rows.append((('→ ' if focused else '  ')+record['id'][:8]+' '+safe_text(record['title'], 40)
                     + (' · new' if record.get('unread') else ''), 'accent' if focused else 'body'))
        # A real failure is red; a running attempt is not (2b).
        failed = record.get('status') == 'error'
        rows.append(('    ['+safe_text(record.get('status', '?'), 20)+']'
                     + (' '+safe_text(record.get('summary', ''), 120) if failed else ''),
                     'alert' if failed else 'soft'))
    return rows


def source_detail(data, key):
    """Rule 2: the hop and address behind a section, and how many tries it took."""
    sources = data.get('sources') if isinstance(data, dict) else None
    detail = sources.get(key) if isinstance(sources, dict) else None
    if not isinstance(detail, dict):
        return []
    lines = [(safe_text(str(detail.get('host', 'Source')), 40)+' at '
              + safe_text(str(detail.get('address', 'unknown address')), 120), 'soft')]
    tries = detail.get('tries')
    if type(tries) is int and not isinstance(tries, bool):
        lines.append((str(tries)+(' try' if tries == 1 else ' tries')+', all failed', 'soft'))
    return lines


def last_good_line(last_good, key):
    """Rule 2: what survived — the time and size of the last read that worked."""
    record = last_good.get(key) if isinstance(last_good, dict) else None
    if not isinstance(record, dict):
        return ('No good read yet in this window.', 'soft')
    return ('Last good '+safe_text(str(record.get('at', '?')), 40)+' · '
            + str(record.get('count', '?'))+' item'+('' if record.get('count') == 1 else 's'), 'soft')


def stale_rows(last_good, key, limit=6):
    """Stale-but-good rows stay on screen rather than blanking the section."""
    record = last_good.get(key) if isinstance(last_good, dict) else None
    rows = record.get('records') if isinstance(record, dict) else None
    if not isinstance(rows, list):
        return []
    return [('#'+safe_text(str(item.get('number', '?')), 12)+'  '+safe_text(item.get('title', ''), 200)+' (stale)', 'soft')
            for item in rows[:limit] if isinstance(item, dict)]


def tray_glance(data, records, selected, task_store):
    lines = [('Waiting on you', 'title')]
    questions = fleet_questions(data.get('fleet'))
    last_good = data.get('last_good') if isinstance(data.get('last_good'), dict) else {}
    if 'fleet' not in data:
        lines.append(('Fleet not loaded · /fleet', 'soft'))
    elif questions is None:
        lines.append(('Questions unavailable — this is not an empty inbox.', 'alert'))
        lines.append((safe_text(data.get('fleet_error', 'No reason recorded.'), 200), 'soft'))
        lines.append(last_good_line(last_good, 'fleet'))
    elif questions:
        lines.append(('! '+str(len(questions))+' Fleet question'+('' if len(questions) == 1 else 's'), 'alert'))
    else:
        lines.append(('Nothing waiting.', 'soft'))
    lines.append(('', 'body'))
    lines.append(('Streams', 'title'))
    lines.extend(stream_rows(records, selected)[:7])
    lines.append(('', 'body'))
    lines.append(('Corner', 'title'))
    inbox = data.get('inbox')
    if not isinstance(inbox, dict):
        lines.append(('Not loaded · /inbox', 'soft'))
    else:
        for key, label in (('issues', 'issue'), ('pulls', 'pull request')):
            if not inbox.get('available', {}).get(key):
                lines.append((label.capitalize()+'s unavailable — not an empty queue.', 'alert'))
                lines.extend(source_detail(inbox, key))
                lines.append(last_good_line(last_good, key))
            else:
                count = len(inbox.get(key) or [])
                lines.append(('▲ '+str(count)+' open '+label+('' if count == 1 else 's'), 'body'))
    lines.append(('', 'body'))
    lines.append(('Tasks', 'title'))
    pending = open_tasks(task_store)
    lines.append(('□ '+str(len(pending))+' open', 'body') if pending
                 else ('None open · /task add', 'soft'))
    return lines


def tray_fleet(data):
    if 'fleet' not in data:
        return [('No Fleet data yet.', 'soft'), ('/fleet loads it.', 'soft')]
    last_good = data.get('last_good') if isinstance(data.get('last_good'), dict) else {}
    if data['fleet'] is None:
        return [('Fleet unavailable.', 'alert'),
                (safe_text(data.get('fleet_error', 'Try /fleet again.'), 300), 'soft'),
                last_good_line(last_good, 'fleet'),
                ('Text chat is unaffected · /fleet retries.', 'soft')]
    snapshot = data['fleet']
    lines = [('Checked '+safe_text(snapshot.get('observed_at', 'just now'), 40), 'soft')]
    questions = fleet_questions(snapshot)
    if questions is None:
        lines.append(('Questions unavailable — this is not an empty inbox.', 'alert'))
        lines.append(last_good_line(last_good, 'fleet'))
    elif not questions:
        lines.append(('No open questions.', 'soft'))
    for question in (questions or [])[:3]:
        lines.append(('', 'body'))
        lines.append(('! ['+safe_text(question.get('id', '?'), 20)+'] '+safe_text(question.get('text', ''), 300), 'alert'))
        for letter, option in zip('abcdefgh', question.get('options', [])[:8]):
            lines.append((letter+'. '+safe_text(option, 120), 'body'))
    if questions:
        lines.append(('', 'body'))
        lines.append(('Answer Fleet question ID with your answer.', 'soft'))
    lines.append(('', 'body'))
    lines.append(('Running', 'title'))
    if not snapshot.get('available', {}).get('attempts'):
        lines.append(('Attempts unavailable.', 'soft'))
        return lines
    attempts = [item for item in snapshot.get('attempts', []) if item.get('state') == 'running'][:6]
    for attempt in attempts:
        lines.append(('◐ '+safe_text(attempt.get('id', '?'), 12)+' '
                      + safe_text(attempt.get('name') or 'Untitled attempt', 120), 'body'))
    if not attempts:
        lines.append(('Nothing running.', 'soft'))
    return lines


def tray_inbox(data):
    inbox = data.get('inbox')
    if not isinstance(inbox, dict):
        return [('Corner work not loaded.', 'soft'), ('/inbox loads it.', 'soft')]
    last_good = data.get('last_good') if isinstance(data.get('last_good'), dict) else {}
    lines = [('Checked '+safe_text(inbox.get('observed_at', 'just now'), 40), 'soft')]
    for key, label in (('issues', 'Issues'), ('pulls', 'Pull requests')):
        lines.append(('', 'body'))
        lines.append((label, 'title'))
        if not inbox.get('available', {}).get(key):
            # One dead source names its hop and keeps its last good rows; it
            # never blanks itself and never blanks the sections beside it.
            lines.append(('Unavailable — not an empty queue.', 'alert'))
            lines.extend(source_detail(inbox, key))
            lines.append(last_good_line(last_good, key))
            lines.extend(stale_rows(last_good, key))
            continue
        records = inbox.get(key) or []
        for item in records[:8]:
            lines.append(('#'+safe_text(str(item.get('number', '?')), 12)+'  '+safe_text(item.get('title', ''), 200), 'body'))
        if not records:
            lines.append(('None open.', 'soft'))
        elif len(records) > 8 or inbox.get('possibly_more', {}).get(key):
            lines.append(('First page only · /issues', 'soft'))
    for error in inbox.get('errors', [])[:4]:
        lines.append((safe_text(str(error.get('source', 'Source'))+': '+str(error.get('message', 'Unavailable')), 200), 'alert'))
    return lines


def tray_windows(data):
    """Rule 3: never overwrite, never silently replay — say exactly what is kept where."""
    info = data.get('windows')
    if not isinstance(info, dict):
        return [('This window owns the conversation.', 'soft'),
                ('Nothing has been overwritten.', 'soft')]
    other = info.get('other_pid')
    return [('The other window is ahead.', 'alert'),
            (safe_text(str(info.get('reason', 'It saved newer context.')), 300), 'body'),
            ('', 'body'),
            ('This window', 'title'),
            ('pid '+safe_text(str(info.get('pid', '?')), 20), 'soft'),
            ('session '+safe_text(str(info.get('session_id') or 'none yet'), 40), 'soft'),
            ('', 'body'),
            ('Other window', 'title'),
            ('pid '+safe_text(str(other if other is not None else 'unknown'), 20)
             + (' · still working' if info.get('other_alive') else ' · no longer running'), 'soft'),
            ('', 'body'),
            ('Kept for you', 'title'),
            ('Transcript, draft, settings and session id:', 'soft'),
            (safe_text(str(info.get('snapshot', 'no snapshot recorded')), 300), 'body'),
            ('', 'body'),
            ('Enter is held. /resume adopts the newer conversation and keeps this draft.', 'alert')]


def tray_tasks(task_store):
    listing = task_store.listing('open')
    if listing.startswith('No matching tasks'):
        return [('No open tasks.', 'soft'), ('/task add Buy tea', 'soft')]
    return [(line, 'body') for line in listing.split('\n')]


def tray_doctor(data):
    checks = data.get('doctor')
    if not isinstance(checks, list):
        return [('No checks run yet.', 'soft'), ('/doctor runs them.', 'soft')]
    lines = []
    for item in checks:
        role = {'unavailable': 'alert', 'attention': 'alert', 'missing': 'alert'}.get(item.get('status'), 'body')
        lines.append((safe_text(item.get('name', '?'), 60), 'title'))
        lines.append((safe_text(str(item.get('status', '?'))+' · '+str(item.get('detail', '')), 200), role))
    return lines


def tray_content(view, data, records, selected, task_store):
    """Pure renderer over already-fetched state; it never opens a connection itself."""
    if view == 'fleet':
        return tray_fleet(data)
    if view == 'inbox':
        return tray_inbox(data)
    if view == 'streams':
        return stream_rows(records, selected)
    if view == 'tasks':
        return tray_tasks(task_store)
    if view == 'doctor':
        return tray_doctor(data)
    if view == 'windows':
        return tray_windows(data)
    return tray_glance(data, records, selected, task_store)


def layout_columns(width):
    """Keep Codsworth and a readable conversation before adding a work panel."""
    left = 18 if width < 80 else (24 if width < 100 else (32 if width < 140 else 40))
    remaining = width-left-4
    side = min(38, remaining-36) if remaining >= 60 else 0
    return left, remaining-side, side


# Markup is for the eye. Spoken literally it becomes "hash one six one three,
# open bracket critical close bracket" -- so the speech path gets prose while the
# transcript keeps every character.
SPOKEN = (
    (re.compile(r'```.*?```', re.S), ' '),            # code fences are not read aloud
    (re.compile(r'`([^`]*)`'), r'\1'),
    (re.compile(r'^[\s>*+-]*[-*_]{3,}\s*$', re.M), ' '),
    (re.compile(r'[│─┬┴╭╮╰╯├┤┼█▁▂▃▄▅▆▇◐◌◍◉◇▸▲□○●⌁⌥]+'), ' '),
    (re.compile(r'#(?=\d)'), ' number '),             # #1613 -> number 1613
    (re.compile(r'^\s{0,3}#{1,6}\s+', re.M), ''),     # markdown headings
    (re.compile(r'\[([^\]]*)\]\([^)]*\)'), r'\1'),  # links: say the words, not the url
    (re.compile(r'[\[\]]'), ' '),
    (re.compile(r'\*\*?([^*]+)\*\*?'), r'\1'),
    (re.compile(r'(?<=\w)_(?=\w)'), ' '),
    (re.compile(r'\s*(?:->|=>|→)\s*'), ' to '),
    (re.compile(r'\s*[—–]\s*'), ', '),
    (re.compile(r'~(?=\d)'), 'about '),
    (re.compile(r'\s*&\s*'), ' and '),
    (re.compile(r'(?<=\w):(?=\w)'), ' '),            # severity:s3 -> severity s3
    (re.compile(r'\s+'), ' '),
)


def speakable(text):
    """The same sentence with its markup read as prose rather than punctuation."""
    for pattern, replacement in SPOKEN:
        text = pattern.sub(replacement, text)
    text = text.strip(' .,;:')
    return text if any(character.isalnum() for character in text) else ''


def clauses(buffer, final=False):
    """Release short complete clauses without buffering the whole assistant turn."""
    ready = []
    while buffer:
        # A short opener ("Yes.") must not hide later sentence boundaries.
        # A long sentence arriving in one model delta must still respect the cap.
        end = next((match for match in re.finditer(r'[.!?;]\s|\n', buffer)
                    if 8 <= match.end() <= 180), None)
        if end:
            stop = end.end()
        elif len(buffer) >= 180:
            stop = buffer.rfind(' ', 0, 180)
            stop = stop if stop > 30 else 180
        elif final:
            stop = len(buffer)
        else:
            break
        chunk, buffer = buffer[:stop].strip(), buffer[stop:].lstrip()
        if chunk:
            ready.append(chunk)
    return ready, buffer


@dataclass
class Settings:
    motion: str = 'full'
    speed: float = 1.0
    voice: str = 'expanded'
    muted: bool = False
    humor: float = .65
    sarcasm: float = .25
    color: str = 'green'
    effort: str = 'none'


class StateConflict(OSError):
    pass


class WorkspaceState:
    """Private per-workspace notes, independent of repository files and model history."""
    def __init__(self, cwd, path=None):
        key = hashlib.sha256(str(Path(cwd).resolve()).encode()).hexdigest()[:20]
        self.path = Path(path) if path else Path.home() / '.local/state/cods/workspaces' / (key + '.json')
        self.panels = {}
        self.tasks = []
        self.task_store = TaskStore()
        self.task_receipts = []
        self.session_id = ''
        self.transcript, self.history = [], []
        self.draft, self.phase = '', 'ready'
        self.context = None
        self.settings = {'color':'green'}
        self.conversation_id = uuid.uuid5(uuid.NAMESPACE_URL, str(self.path.resolve())+'#legacy').hex
        self.title = 'New conversation'
        self.conversations = ConversationStore(self.path)
        self.cursor, self.scroll = 0, 0
        # An older state file has neither tray key and must load unchanged.
        self.tray_open, self.tray_view = False, 'glance'
        # Rule 2: when the last good read happened, so a later failure can say.
        self.tray_last_good = {}
        self.owner_pid = None
        self.loaded_phase = 'ready'
        self.fingerprint = None
        self.recovery_path = self.path.parent / 'recovery' / (self.path.stem+'-'+uuid.uuid4().hex+'.json')
        if self.path.exists():
            try:
                raw = self.path.read_bytes()
                self.fingerprint = hashlib.sha256(raw).hexdigest()
                data = json.loads(raw)
                if data.get('conversation_id'):
                    self.conversation_id = uuid.UUID(data['conversation_id']).hex
                self.title = safe_text(data.get('title', 'New conversation'), 80).replace('\n', ' ')
                saved_context = data.get('context')
                if isinstance(saved_context,dict):
                    self.context = context_usage(saved_context.get('used'),saved_context.get('limit'))
                self.transcript = self.bounded_transcript(data.get('transcript', []))
                self.history = [safe_text(item, 8000) for item in data.get('history', []) if isinstance(item, str)][-100:]
                self.history = self.history[-max(1, 64000 // max(1, max(map(len, self.history), default=1))):]
                self.draft = safe_text(data.get('draft', ''), 8000)
                self.cursor = max(0, min(len(self.draft), int(data.get('cursor', len(self.draft)))))
                self.scroll = max(0, min(100000, int(data.get('scroll', 0))))
                if isinstance(data.get('tray_open'), bool):
                    self.tray_open = data['tray_open']
                if data.get('tray_view') in TRAY_VIEWS:
                    self.tray_view = data['tray_view']
                raw_last_good = data.get('tray_last_good')
                if isinstance(raw_last_good, dict):
                    for name in LAST_GOOD_SECTIONS:
                        record = raw_last_good.get(name)
                        if not isinstance(record, dict):
                            continue
                        at, count = record.get('at'), record.get('count')
                        if isinstance(at, str) and type(count) is int and 0 <= count <= 100000:
                            self.tray_last_good[name] = {'at': safe_text(at, 40), 'count': count}
                self.phase = data.get('phase', 'ready')
                self.loaded_phase = self.phase
                self.owner_pid = data.get('owner_pid')
                raw_settings = data.get('settings', {})
                if isinstance(raw_settings, dict):
                    for name, low, high in [('speed', .5, 2), ('humor', 0, 1), ('sarcasm', 0, 1)]:
                        value = raw_settings.get(name)
                        if isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high:
                            self.settings[name] = value
                    if raw_settings.get('voice') in ('pilot', 'expanded'):
                        self.settings['voice'] = raw_settings['voice']
                    if isinstance(raw_settings.get('muted'), bool):
                        self.settings['muted'] = raw_settings['muted']
                    if raw_settings.get('motion') in ('full', 'reduced'):
                        self.settings['motion'] = raw_settings['motion']
                    if raw_settings.get('color') in COLOR_OPTIONS:
                        self.settings['color'] = raw_settings['color']
                    if raw_settings.get('effort') in EFFORT_LEVELS:
                        self.settings['effort'] = raw_settings['effort']
                if data.get('session_id'):
                    self.session_id = uuid.UUID(str(data['session_id'])).hex
                self.tasks = [safe_text(task, 500) for task in data.get('tasks', []) if isinstance(task, str)][:100]
                self.task_store = TaskStore(data.get('task_records', []))
                self.task_store.reconcile(self.tasks)
                self.task_receipts = [safe_text(item, 200) for item in data.get('task_receipts', []) if isinstance(item, str)][-4:]
                for name, panel in data.get('panels', {}).items():
                    if name in ('today', 'tasks', 'notes') and isinstance(panel, dict):
                        self.panels[name] = dict(title=safe_text(panel.get('title', name), 80),
                                                 content=safe_text(panel.get('content', ''), 2400))
            except (ValueError, TypeError, AttributeError, OSError):
                pass

    @staticmethod
    def bounded_transcript(items):
        result, remaining = [], 64000
        if not isinstance(items, list):
            return result
        for item in reversed(items[-100:]):
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                continue
            if not all(isinstance(value, str) for value in item):
                continue
            label, content = safe_text(item[0], 80), safe_text(item[1], min(30000, remaining))
            result.append((label, content))
            remaining -= len(content)
            if remaining <= 0:
                break
        return result[::-1]

    def changed(self):
        try:
            current = hashlib.sha256(self.path.read_bytes()).hexdigest()
        except FileNotFoundError:
            current = None
        return current != self.fingerprint

    def owner_alive(self):
        if not isinstance(self.owner_pid, int) or self.owner_pid <= 0 or self.owner_pid == os.getpid():
            return False
        try:
            os.kill(self.owner_pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    @staticmethod
    def atomic_write(path, payload):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix='.cods-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(payload)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def save(self):
        self.task_store.reconcile(self.tasks)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(str(self.path)+'.lock', os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            payload = (json.dumps(dict(version=3, tasks=self.tasks, task_records=self.task_store.records, task_receipts=self.task_receipts, panels=self.panels, session_id=self.session_id,
                    transcript=self.bounded_transcript(self.transcript), draft=self.draft[:8000],
                    history=self.history[-100:], settings=self.settings, phase=self.phase,
                    cursor=self.cursor, scroll=self.scroll, owner_pid=os.getpid(),
                    tray_open=self.tray_open, tray_view=self.tray_view,
                    tray_last_good=self.tray_last_good,
                    context={'used':self.context[0],'limit':self.context[1]} if self.context else None,
                    conversation_id=self.conversation_id, title=self.title, updated_at=time.time()), indent=2)+'\n').encode()
            if self.changed() or (self.loaded_phase in ('thinking', 'speaking', 'tool', 'recording', 'transcribing') and self.owner_alive()):
                self.atomic_write(self.recovery_path, payload)
                raise StateConflict('Another window saved newer context. Use /resume. Your changes are safe in '+str(self.recovery_path))
            self.atomic_write(self.path, payload)
            self.fingerprint = hashlib.sha256(payload).hexdigest()
            self.owner_pid, self.loaded_phase = os.getpid(), self.phase
            self.conversations.save(json.loads(payload))


def setting_command(command, settings):
    name, _, value = command.strip().partition(' ')
    if name == '/color' and value:
        value = value.strip().lower()
        if value not in COLOR_OPTIONS:
            raise ValueError('Choose a color: '+', '.join(COLOR_OPTIONS))
        settings.color = value
        return 'Conversation color: '+value
    if name == '/motion':
        if value not in ('full', 'reduced'):
            raise ValueError('Use /motion full or /motion reduced')
        settings.motion = value
        return 'Animation motion: '+value
    if name == '/speed':
        try:
            speed = float(value)
        except ValueError:
            raise ValueError('Use /speed 0.5 through 2.0') from None
        if not .5 <= speed <= 2:
            raise ValueError('Speed must be between 0.5 and 2.0')
        settings.speed = speed
        return f'Speech speed: {speed:g}×'
    if name == '/voice':
        if value not in ('pilot', 'expanded'):
            raise ValueError('Use /voice pilot or /voice expanded')
        settings.voice = value
        return f'Voice: {value}'
    if name == '/mute':
        settings.muted = not settings.muted
        return 'Speech muted' if settings.muted else 'Speech enabled'
    if name == '/effort':
        value = value.strip().lower()
        if not value:
            return None
        if value not in EFFORT_LEVELS:
            raise ValueError('Choose effort: '+', '.join(EFFORT_LEVELS))
        settings.effort = value
        return 'Reasoning effort: '+value
    if name in ('/humor', '/sarcasm'):
        try:
            amount = float(value)
        except ValueError:
            raise ValueError(f'Use {name} 0 through 1') from None
        if not 0 <= amount <= 1:
            raise ValueError(f'{name} must be between 0 and 1')
        setattr(settings, name[1:], amount)
        return f'{name[1:].capitalize()}: {amount:g}'
    return None


@dataclass
class Turn:
    local_id: int
    cancelled: threading.Event = field(default_factory=threading.Event)
    connections: dict = field(default_factory=dict)
    sockets: dict = field(default_factory=dict)
    players: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    session_id: str = ''
    turn_id: str = ''
    trace: object = None
    # Every failure names its hop, its address and the number of tries it made.
    chat_attempts: int = 0
    voice_attempts: int = 0

    def close_io(self, voice_only=False):
        if self.trace:
            self.trace('close_io', voice_only=voice_only,
                       callers=[frame.name for frame in traceback.extract_stack(limit=5)[:-1]])
        with self.lock:
            for connection, kind in list(self.connections.items()):
                if voice_only and kind != 'voice':
                    continue
                try:
                    active_socket = self.sockets.get(connection) or connection.sock
                    if active_socket:
                        active_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
            for player in self.players:
                if player.poll() is None:
                    try:
                        if self.trace:
                            self.trace('player_kill', cause='close_io', voice_only=voice_only)
                        player.kill()
                    except OSError:
                        pass  # The audio process can exit between poll and kill.

    def cancel(self):
        self.cancelled.set()
        self.close_io()


# Failures that mean no response ever started: nothing to replay, safe to resend.
CONNECTION_ERRORS = (ConnectionResetError, ConnectionRefusedError, ConnectionAbortedError,
                     BrokenPipeError, http.client.RemoteDisconnected)
CONNECTION_TRIES = 3


def tunnel_hint(error, address):
    """Say what a connection failure on the local tunnel end really means.

    The launcher forwards a loopback port over SSH and tells us where to with
    CODS_TUNNEL_TARGET. When that loopback hop resets or refuses, the tunnel
    itself answered and the machine behind it did not, so name the machine
    rather than leaving 127.0.0.1 to take the blame.
    """
    if not isinstance(error, CONNECTION_ERRORS):
        return ''
    host = urlsplit(address).hostname if '://' in str(address) else str(address).split(':')[0]
    if host not in ('127.0.0.1', 'localhost', '::1'):
        return ''
    target = safe_text(os.environ.get('CODS_TUNNEL_TARGET', ''), 120).strip()
    if isinstance(error, ConnectionRefusedError):
        return 'nothing is listening on the local port; the SSH tunnel is not up'
    if target:
        return ('the SSH tunnel is up but nothing answered at '+target
                + ' behind it; is the voice gateway container running?')
    return 'the SSH tunnel is up but its far end did not answer'


class ServiceError(RuntimeError):
    def __init__(self, status, detail):
        super().__init__(f'Service HTTP {status}: {detail}')
        self.status = status


class PlaybackDiagnostics:
    """Drain ffplay stderr continuously, retaining only a small diagnostic tail."""
    def __init__(self, stream):
        self.buffer = bytearray()
        self.lock = threading.Lock()
        self.thread = None
        if stream is not None:
            def read():
                try:
                    with stream:
                        while True:
                            data = stream.read(4096)
                            if not data:
                                break
                            with self.lock:
                                self.buffer.extend(data)
                                del self.buffer[:-4096]
                except (OSError, ValueError):
                    pass
            self.thread = threading.Thread(target=read, daemon=True)
            self.thread.start()

    def message(self):
        if self.thread:
            self.thread.join(.2)
        with self.lock:
            return safe_text(bytes(self.buffer).decode('utf-8', errors='replace')[-600:], 600).strip()


class PlaybackLevels:
    """Amplitude track taken from the PCM actually handed to the player.

    Writes run up to two seconds ahead of what is audible, so every window is
    stamped with the moment it will be *heard* rather than the moment it was
    written. Reading the track back by wall clock therefore shows the level of
    the speech the listener is hearing now, not the level being synthesised.
    """
    WINDOW = 512  # samples; about 11ms at 48kHz
    GLYPHS = '\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588'

    def __init__(self, capacity=1024):
        self.lock = threading.Lock()
        self.samples = collections.deque(maxlen=capacity)

    def clear(self):
        with self.lock:
            self.samples.clear()

    def add(self, pcm, audible_at):
        """Record the peak of each window of `pcm`, stamped with its play time."""
        step = self.WINDOW*2
        found = []
        for offset in range(0, len(pcm)//2*2, step):
            block = pcm[offset:offset+step]
            values = array.array('h')
            values.frombytes(block[:len(block)//2*2])
            if not values:
                continue
            peak = max(max(values), -min(values))/32768
            found.append((audible_at+offset/2/48000, peak))
        if found:
            with self.lock:
                self.samples.extend(found)

    def window(self, now, cells, span=1.6):
        """`cells` glyphs covering the last `span` seconds, or None when silent."""
        if cells <= 0:
            return None
        with self.lock:
            while self.samples and self.samples[0][0] < now-span-1:
                self.samples.popleft()
            recent = [pair for pair in self.samples if now-span <= pair[0] <= now]
        if not recent:
            return None
        buckets = [0.0]*cells
        start = now-span
        for at, level in recent:
            index = min(cells-1, max(0, int((at-start)/span*cells)))
            buckets[index] = max(buckets[index], level)
        return ''.join('\u2500' if value <= 0 else self.GLYPHS[min(7, int(value*8))] for value in buckets)


def duration(seconds):
    seconds = max(0, int(seconds or 0))
    return f'{seconds}s' if seconds < 60 else f'{seconds//60}m{seconds%60:02d}s'


class PlaybackLead:
    """Bounded startup lead when accelerated PCM arrives slower than consumption.

    It does not wait for a completed sentence: ordinary 1× starts immediately,
    while faster speech measures incoming media cadence and caps held PCM at 2s.
    """
    def __init__(self, text, speed):
        self.speed = speed
        self.estimated_duration = max(.3, len(text) / 15 / speed)
        self.buffer = bytearray()
        self.first_at = None
        self.first_duration = 0
        self.started = speed <= 1
        self.target_seconds = 0

    def push(self, pcm, now):
        if self.started:
            return pcm
        self.buffer.extend(pcm)
        duration = len(self.buffer) / 96000
        if self.first_at is None:
            self.first_at, self.first_duration = now, len(pcm)/96000
        observed_audio = duration-self.first_duration
        if observed_audio < .2:
            return self.finish() if duration >= 2 else b''
        production_ratio = (now-self.first_at) / observed_audio
        # The amount needed is D*(1 - 1/R), with a small jitter allowance.
        # Fast production needs only a short sample window, not a fixed 2s wait.
        self.target_seconds = min(2, max(.2, self.estimated_duration *
                                        max(0, 1-1/max(1, production_ratio)) + .15))
        return self.finish() if duration >= self.target_seconds else b''

    def finish(self):
        data = bytes(self.buffer)
        self.buffer.clear()
        self.started = True
        return data


class Client:
    def __init__(self, url, settings, events, player_path=None):
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError('Service URL must be http:// or https://')
        self.parsed, self.settings, self.events = parsed, settings, events
        self.player_path = player_path or shutil.which('ffplay')
        self.active = None
        self.sequence = 0
        self.session_id = ''
        self.workspace_context = {}
        self.audio_observer = None  # Optional diagnostic hook; never records by default.
        self.levels = PlaybackLevels()  # Amplitude of the speech currently audible.
        self.audio_trace_path = os.environ.get('CODS_AUDIO_TRACE')

    def address(self):
        """The hop this client actually talks to, without any credential part."""
        port = self.parsed.port
        return self.parsed.scheme+'://'+self.parsed.hostname+(':'+str(port) if port else '')

    @staticmethod
    def hop_detail(hop, address, attempts, error=''):
        """Name the hop and its address, and count the tries. Never a traceback."""
        tries = attempts if isinstance(attempts, int) and attempts > 0 else 1
        line = safe_text(error, 400).strip()
        hint = tunnel_hint(error, address)
        return ((line+' · ' if line else '')+hop+' at '+address+' · '
                + str(tries)+(' try' if tries == 1 else ' tries')+(' · '+hint if hint else ''))

    def observe_audio(self, turn, event, **values):
        record = dict(event=event, turn=turn.local_id, seconds=time.monotonic(), **values)
        if self.audio_observer:
            self.audio_observer(record)
        if self.audio_trace_path:
            try:
                descriptor = os.open(self.audio_trace_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(descriptor, (json.dumps(record)+'\n').encode())
                finally:
                    os.close(descriptor)
            except OSError:
                pass

    def emit(self, turn, kind, **payload):
        self.events.put((turn.local_id, dict(type=kind, **payload)))

    def stream(self, path, payload, turn, kind='chat', content_type='application/json'):
        if turn.trace is None:
            turn.trace = lambda event, **values: self.observe_audio(turn, event, **values)
        cls = http.client.HTTPSConnection if self.parsed.scheme == 'https' else http.client.HTTPConnection
        connection = cls(self.parsed.hostname, self.parsed.port, timeout=30)
        with turn.lock:
            if turn.cancelled.is_set():
                return
            turn.connections[connection] = kind
        try:
            body = json.dumps(payload) if content_type == 'application/json' else payload
            connection.request('POST', self.parsed.path.rstrip('/') + path, body,
                               {'Content-Type': content_type, 'Accept': 'application/x-ndjson'})
            with turn.lock:
                # HTTPConnection clears .sock when the response is close-delimited;
                # keep its underlying socket so interruption still breaks readline.
                turn.sockets[connection] = connection.sock
                if turn.cancelled.is_set():
                    connection.close()
                    return
            response = connection.getresponse()
            if response.status != 200:
                detail = safe_text(response.read(4096).decode('utf-8', errors='replace'), 500)
                raise ServiceError(response.status, detail)
            while not turn.cancelled.is_set():
                line = response.readline(2 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 2 * 1024 * 1024:
                    raise RuntimeError('Service event exceeds size limit')
                if line.strip():
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise RuntimeError('Invalid service event')
                    yield event
        finally:
            with turn.lock:
                turn.connections.pop(connection, None)
                turn.sockets.pop(connection, None)
            connection.close()

    def chat_events(self, payload, turn):
        # An interrupted upstream turn can need a moment to release its session,
        # and a tunnel or gateway can drop the socket before answering at all.
        # Both are safe to resend as long as no event has reached the caller;
        # once one has, a retry would replay text the screen already shows.
        deadline = time.monotonic() + 2
        while not turn.cancelled.is_set():
            turn.chat_attempts += 1
            received = False
            try:
                for event in self.stream('/api/chat', payload, turn):
                    received = True
                    yield event
                return
            except ServiceError as error:
                if error.status != 409 or received or time.monotonic() >= deadline:
                    raise
                turn.cancelled.wait(.1)
            except CONNECTION_ERRORS:
                if received or turn.chat_attempts >= CONNECTION_TRIES:
                    raise
                turn.cancelled.wait(.3)

    def interrupt(self, reason='explicit'):
        turn = self.active
        if not turn:
            return
        self.observe_audio(turn, 'cancel', reason=reason,
                           callers=[frame.name for frame in traceback.extract_stack(limit=5)[:-1]])
        turn.cancel()
        # Disconnect is primary cancellation. Explicit endpoint also invalidates
        # the server turn if its upstream stream hasn't observed the close yet.
        if turn.session_id:
            def notify():
                try:
                    cancellation = Turn(-1)
                    list(self.stream('/api/chat/cancel', {'session_id': turn.session_id,
                              'turn_id': turn.turn_id or None}, cancellation))
                except Exception:
                    pass
            threading.Thread(target=notify, daemon=True).start()
        self.active = None

    def submit(self, text, images=None):
        self.interrupt(reason='new_turn')
        self.sequence += 1
        turn = Turn(self.sequence)
        self.active = turn
        kwargs = {'images': images} if images else {}
        threading.Thread(target=self.run, args=(turn, text), kwargs=kwargs, daemon=True).start()
        return turn.local_id

    def run(self, turn, text, images=None):
        speech = queue.Queue(maxsize=4)
        speaker = threading.Thread(target=self.speak, args=(turn, speech), daemon=True)
        speaker.start()
        def enqueue(item):
            while not turn.cancelled.is_set():
                try:
                    speech.put(item, timeout=.05)
                    return
                except queue.Full:
                    continue
        pending, complete = '', False
        try:
            payload = dict(text=text, humor=self.settings.humor, sarcasm=self.settings.sarcasm,
                           effort=self.settings.effort)
            if self.workspace_context:
                payload['workspace_context'] = self.workspace_context
            if self.session_id:
                payload['session_id'] = self.session_id
            if images:
                payload['images'] = images
            for event in self.chat_events(payload, turn):
                if turn.cancelled.is_set():
                    break
                kind = event.get('type')
                if kind == 'start':
                    turn.session_id = event['session_id']
                    turn.turn_id = event['turn_id']
                    self.session_id = turn.session_id
                    self.emit(turn, 'session', session_id=turn.session_id,
                              model=event.get('model', ''), runtime=event.get('runtime', ''))
                elif kind == 'text':
                    delta = safe_text(event.get('delta', ''), 8000)
                    self.emit(turn, 'text', delta=delta)
                    pending += delta
                    ready, pending = clauses(pending)
                    for clause in ready:
                        spoken = speakable(clause)
                        if spoken:
                            enqueue(spoken)
                elif kind == 'action':
                    self.emit(turn, 'action', action=event)
                elif kind == 'tool':
                    self.emit(turn, 'tool', tool=event)
                elif kind == 'error':
                    raise RuntimeError(event.get('message', 'Chat failed'))
                elif kind == 'done':
                    complete = True
                    if type(event.get('context_tokens')) is int:
                        self.emit(turn, 'context', used=event['context_tokens'],
                                  limit=event.get('context_limit'))
            if not complete and not turn.cancelled.is_set():
                raise RuntimeError('Chat stream ended before completion; retry your message')
            for clause in clauses(pending, final=True)[0]:
                spoken = speakable(clause)
                if spoken:
                    enqueue(spoken)
        except Exception as error:
            if not turn.cancelled.is_set():
                self.emit(turn, 'error', channel='chat', message=self.hop_detail(
                    'Hermes gateway', self.address(), turn.chat_attempts, error))
        finally:
            enqueue(None)
            while speaker.is_alive() and not turn.cancelled.is_set():
                speaker.join(.05)
            if not turn.cancelled.is_set():
                self.emit(turn, 'complete')

    def speak(self, turn, speech):
        # Synthesis runs ahead through a bounded PCM queue. One ffplay process
        # consumes the entire reply; sentence boundaries never close its pipe.
        pcm_queue = queue.Queue(maxsize=16)
        audio_failed = threading.Event()
        player = diagnostics = lead = None
        notice_sent = False
        queued_seconds, clock_at = 0.0, time.monotonic()

        def put(event, audio=False):
            while not turn.cancelled.is_set():
                if audio and (audio_failed.is_set() or self.settings.muted):
                    return False
                try:
                    pcm_queue.put(event, timeout=.05)
                    return True
                except queue.Full:
                    continue
            return False

        def produce():
            while not turn.cancelled.is_set():
                try:
                    clause = speech.get(timeout=.1)
                except queue.Empty:
                    continue
                if clause is None:
                    break
                # Keep draining text after a device/service failure. Otherwise the
                # chat producer can deadlock on its own four-clause queue.
                if audio_failed.is_set() or self.settings.muted or not self.player_path:
                    continue
                speed = self.settings.speed
                try:
                    self.observe_audio(turn, 'request', text=clause, speed=speed)
                    put(dict(type='clause', text=clause, speed=speed))
                    completed = False
                    for event in self.voice_events(dict(text=clause, style='', seed=42,
                        voice=self.settings.voice, speed=speed), turn):
                        if audio_failed.is_set() or self.settings.muted or turn.cancelled.is_set():
                            break
                        kind = event.get('type')
                        if kind == 'start' and event.get('sample_rate', 48000) != 48000:
                            raise RuntimeError('Unsupported PCM sample rate')
                        if kind == 'audio':
                            pcm = base64.b64decode(event['pcm'], validate=True)
                            if not pcm or len(pcm) % 2:
                                raise RuntimeError('Invalid PCM chunk')
                            arrived = time.monotonic()
                            self.observe_audio(turn, 'pcm_received', bytes=len(pcm))
                            for offset in range(0, len(pcm), 16384):
                                if not put(dict(type='pcm', data=pcm[offset:offset+16384], at=arrived), audio=True):
                                    break
                        elif kind == 'done':
                            completed = True
                            self.observe_audio(turn, 'generation_done', id=event.get('id'))
                            put(dict(type='clause_end'))
                        elif kind == 'error':
                            raise RuntimeError(event.get('message', 'Speech failed'))
                    if not completed and not turn.cancelled.is_set() and not self.settings.muted and not audio_failed.is_set():
                        raise RuntimeError('Speech stream ended before completion')
                except Exception as error:
                    if not turn.cancelled.is_set() and not self.settings.muted and not audio_failed.is_set():
                        audio_failed.set()
                        put(dict(type='error', error=error))
            put(dict(type='end'))

        producer = threading.Thread(target=produce, daemon=True)
        producer.start()

        def cleanup(kill=False):
            nonlocal player, diagnostics, lead, queued_seconds, clock_at
            if player:
                if kill and player.poll() is None:
                    try:
                        self.observe_audio(turn, 'player_kill', cause='cleanup', cancelled=turn.cancelled.is_set())
                        player.kill()
                    except OSError:
                        pass
                self.close_player_input(player)
                try:
                    player.wait(timeout=60 if not kill else 2)
                except subprocess.TimeoutExpired:
                    self.observe_audio(turn, 'player_kill', cause='playback_timeout')
                    player.kill()
                    player.wait()
                self.observe_audio(turn, 'player_end', exit_code=player.returncode)
                detail = diagnostics.message() if diagnostics else ''
                code = player.returncode
                with turn.lock:
                    turn.players.remove(player)
                player = diagnostics = None
                lead = None
                self.levels.clear()
                queued_seconds, clock_at = 0.0, time.monotonic()
                return code, detail
            return 0, ''

        def fail(error):
            nonlocal notice_sent
            audio_failed.set()
            turn.close_io(voice_only=True)
            _, detail = cleanup(kill=True)
            if not notice_sent and not turn.cancelled.is_set() and not self.settings.muted:
                self.emit(turn, 'notice', channel='voice', message='Speech unavailable: ' + self.hop_detail(
                    'Voice service', self.address(), turn.voice_attempts, detail or safe_text(error, 300)))
                notice_sent = True

        def play(pcm):
            nonlocal player, diagnostics, queued_seconds, clock_at
            if not pcm or audio_failed.is_set() or turn.cancelled.is_set() or self.settings.muted:
                return
            if player is None:
                with turn.lock:
                    if turn.cancelled.is_set():
                        return
                    player = subprocess.Popen([self.player_path, '-nodisp', '-autoexit', '-loglevel', 'error',
                        '-probesize', '32', '-analyzeduration', '0',
                        '-f', 's16le', '-ar', '48000', '-ch_layout', 'mono', '-i', 'pipe:0'],
                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    diagnostics = PlaybackDiagnostics(getattr(player, 'stderr', None))
                    turn.players.append(player)
                clock_at = time.monotonic()
                self.observe_audio(turn, 'player_start', lead_target_seconds=lead.target_seconds if lead else 0)
                self.emit(turn, 'speaking')
            for offset in range(0, len(pcm), 16384):
                part = pcm[offset:offset+16384]
                # Do not let ffplay's internal demux queue absorb an entire reply.
                # Pace writes to at most about two seconds ahead of consumption.
                while True:
                    now = time.monotonic()
                    queued_seconds = max(0, queued_seconds-(now-clock_at))
                    clock_at = now
                    if turn.cancelled.is_set() or audio_failed.is_set() or self.settings.muted:
                        return
                    if queued_seconds+len(part)/96000 <= 2:
                        break
                    turn.cancelled.wait(.02)
                # Stamp the level with when this PCM becomes audible, not now.
                self.levels.add(part, clock_at+queued_seconds)
                player.stdin.write(part)
                player.stdin.flush()
                queued_seconds += len(part)/96000
                self.observe_audio(turn, 'pcm_written', bytes=len(part))

        try:
            while not turn.cancelled.is_set():
                try:
                    event = pcm_queue.get(timeout=.05)
                except queue.Empty:
                    continue
                kind = event['type']
                if kind == 'end':
                    break
                if kind == 'error':
                    fail(event['error'])
                    continue
                if audio_failed.is_set():
                    continue
                if self.settings.muted:
                    cleanup(kill=True)
                    continue
                try:
                    if kind == 'clause' and lead is None:
                        lead = PlaybackLead(event['text'], event['speed'])
                    elif kind == 'pcm':
                        play(lead.push(event['data'], event['at']) if lead else event['data'])
                    elif kind == 'clause_end' and lead is not None and not lead.started:
                        # A short first clause still plays promptly. The lead is
                        # never recreated at subsequent sentence boundaries.
                        play(lead.finish())
                except Exception as error:
                    if self.settings.muted or turn.cancelled.is_set():
                        cleanup(kill=True)
                    else:
                        fail(error)
            if not turn.cancelled.is_set():
                code, detail = cleanup()
                if code and not self.settings.muted and not notice_sent:
                    self.emit(turn, 'notice', channel='voice', message='Speech unavailable: ' + self.hop_detail(
                        'Local audio player', self.player_path or 'ffplay', 1, detail or 'Playback exited with an error'))
        finally:
            cleanup(kill=True)
            # Cancellation unblocks both queue puts and the upstream voice socket.
            # Device failures keep the producer draining clauses until chat ends.
            while producer.is_alive() and not turn.cancelled.is_set():
                producer.join(.05)

    @staticmethod
    def close_player_input(player):
        if player.stdin and not getattr(player.stdin, 'closed', False):
            try:
                player.stdin.close()
            except OSError:
                pass

    def voice_events(self, payload, turn):
        """Wait briefly for a cancelled generation to release the TTS lock."""
        deadline = time.monotonic() + 3
        received = False
        while not turn.cancelled.is_set() and not self.settings.muted:
            turn.voice_attempts += 1
            try:
                for event in self.stream('/api/generate', payload, turn, kind='voice'):
                    received = True
                    yield event
                return
            except ServiceError as error:
                # Only an unstarted busy request can be replayed; never synthesize
                # a clause again after any part of it may have reached playback.
                if error.status != 409 or received or time.monotonic() >= deadline:
                    raise
                turn.cancelled.wait(.1)


HELP = '''Enter: send a message · Esc: interrupt · Ctrl-C twice: quit (within 3 seconds)
Type / to browse commands · arrows choose · Tab completes
Hold Space with empty input: record; release: transcribe and send
Requires Ghostty/Kitty key-release support; normal typed spaces remain unchanged
/effort none|low|medium|high   Reasoning effort for the next turn
/speed           Rate slider · /speed 0.5..2 sets tempo directly
/voice expanded|pilot     Choose trained voice
/mute            Toggle speech, continue text chat
/humor 0..1      /sarcasm 0..1
/todo Buy tea    Add a workspace task · /todos show tasks
/done 1          Complete a captured task by number
/files           List current directory (read only)
/plugins         Native tools and external integration status
/fleet           Refresh Fleet progress · /questions shows its question inbox
/watch on|off    Watch Fleet changes every 30 seconds · /updates shows new events
/bug DESCRIPTION File a Codsworth bug in its Gitea repo (no automatic log attachment)
/joke            A little wasteland wit · /tea for a virtual cuppa
/doctor          Read-only service and local audio-tool checks
/settings        Show current delivery, motion and Fleet-watch settings
/new             Start a fresh conversation · /help
/sessions        Saved conversations · /switch ID · /rename TITLE
/color [NAME]   Choose a saved conversation accent · /color cyan
/tray [VIEW]    Toggle the tray: glance, fleet, inbox, streams, tasks, doctor, windows
/windows        Which window is ahead, and where this one's work is kept
/streams        Parallel workstreams · /stream new TITLE
/focus ID|home  Change message target · Ctrl+Tab cycles focus
/inbox          Corner issues, PRs and Fleet activity
/issues [open|closed|all] [page] · /issues NUMBER for details
/bugs           Codsworth maintenance issues · /bug TEXT files one
/tasks all|archived  Task IDs, priorities and due dates
/task add TEXT [--priority high] [--due YYYY-MM-DD]
/task archive|restore ID · /task done|reopen ID · /task priority ID low|normal|high · /task due ID DATE|none
/motion full|reduced      Animation preference
/resume          Load newer context saved by another window; keep your draft
/copy            Copy conversation · /export PATH saves a new text file
PageUp / PageDown scroll the conversation'''


def composer_layout(text, cursor, width):
    """Wrapped draft plus display-cell cursor position (including wide Unicode)."""
    width = max(2, width)
    lines, line, column, positions = [], '', 0, []
    for character in text:
        cells = 0 if unicodedata.combining(character) else (2 if unicodedata.east_asian_width(character) in ('W','F') else 1)
        if character != '\n' and column+cells > width:
            lines.append(line); line, column = '', 0
        positions.append((len(lines),column))
        if character == '\n':
            lines.append(line); line, column = '', 0
        else:
            line += character; column += cells
    if column >= width:
        lines.append(line); line, column = '', 0
    positions.append((len(lines),column))
    lines.append(line)
    row,col = positions[max(0,min(cursor,len(text))) ]
    return lines,row,col


def composer_metrics(height, draft_line_count, image_count, graphics):
    """Rows of the composer box: draft, optional image strip, inner meta line."""
    shown = min(max(1, draft_line_count), 5 if height >= 36 else 3)
    previews = 4 if image_count and graphics and height >= 32 else (1 if image_count and height >= 24 else 0)
    meta = 1 if height >= 24 else 0
    pad = 1 if height >= 24 and not previews else 0
    top = height - shown - 2 - meta
    return shown, top, top - previews - pad - 1, previews, meta, pad


def composer_meta(target, meters, mode, width, left, inner):
    """Fit target, context meter and mode on one row.

    Returns (kind, start, shown, meter, text) where kind is 'inner' (right-aligned
    group), 'rule' (padded target on the compact top rule), or 'split' (target
    left, meter right) when nothing else fits.
    """
    right = width - 4
    for meter in meters:
        room = max(8, right - left)
        needed = len(target) + 3 + len(meter) + 3 + len(mode)
        shown = target
        if needed > room:
            keep = room - len(meter) - len(mode) - 8
            shown = (target[:max(1, keep)] + '…') if keep > 0 else ''
        parts = [part for part in (shown, meter, mode) if part]
        text = ' · '.join(parts)
        start = right - len(text)
        if start < left:
            continue
        if inner:
            return 'inner', start, shown, meter, text
        label = (' ' + target + ' ')[:max(0, start - 6)]
        if target in label:
            return 'rule', start, label, meter, text
    meter = meters[-1]
    return 'split', max(left, right - len(meter)), target, meter, meter


def footer_keys(draft, muted, tray_key, image_hint):
    if image_hint:
        return image_hint
    if draft:
        return f'Enter:send | Shift+Enter:newline | Shift+Tab:mode | {tray_key}:tray'
    if muted:
        return f'Enter:send | Shift+Tab:unmute | {tray_key}:tray'
    return f'Enter:send | hold Space:talk | Shift+Tab:mode | {tray_key}:tray'


class Terminal:
    @property
    def input(self):
        return self._input

    @input.setter
    def input(self, value):
        self._input = value
        self.cursor = len(value)

    def edit_input(self, text, cursor):
        self.input = text[:8000]
        self.cursor = max(0,min(cursor,len(self.input)))
        self.history_index = None
        if hasattr(self, 'mark_dirty'): self.mark_dirty()

    def recall_input(self, direction):
        if hasattr(self, 'workstreams') and self.workstreams.selected:
            return
        if not self.input_history:
            return
        if self.history_index is None:
            if direction > 0:
                return
            self.history_draft = self.input
            self.history_index = len(self.input_history)
        self.history_index = max(0,min(len(self.input_history),self.history_index+direction))
        self.input = self.history_draft if self.history_index == len(self.input_history) else self.input_history[self.history_index]
        if hasattr(self, 'mark_dirty'): self.mark_dirty()

    def __init__(self, url, muted=False, state_path=None):
        self.events = queue.Queue()
        self.settings = Settings(muted=muted or not bool(shutil.which('ffplay')))
        self.effort_menu = False
        self.client = Client(url, self.settings, self.events)
        self.cwd = Path.cwd()
        self.state = WorkspaceState(self.cwd, state_path)
        for name, value in self.state.settings.items():
            setattr(self.settings, name, value)
        if muted or not shutil.which('ffplay'):
            self.settings.muted = True
        self.client.session_id = self.state.session_id
        self.backend_label = 'Conversation service'
        self.transcript = self.state.transcript or [('Codsworth', 'At your service. What shall we attend to today?')]
        self.panel = ('Today', 'Tell me what needs doing. I can help plan the day and keep notes here.\n\n/help lists controls. /plugins opens the integration TODO platter.')
        self.tasks = self.state.tasks
        if self.state.panels:
            panel = self.state.panels.get('today', next(iter(self.state.panels.values())))
            self.panel = (panel['title'], panel['content'])
        self.input = self.state.draft
        self.cursor = self.state.cursor
        self.input_history = self.state.history
        self.history_index = None
        self.history_draft = ''
        self.expression = 'neutral'
        self.status = 'Ready'
        self.phase = 'ready'
        self._expanded_messages = set()
        self._selection_start = None
        self._selection_end = None
        self._message_cells = {}
        self._message_hits = {}
        self._phase_started = time.monotonic()
        self._reply_pending = False
        self._turn_started = None
        self._silent_since = None
        self.context = self.state.context
        self._dirty = False
        self._last_save = 0.0
        self._conflict_draft = None
        self._task_actions_seen = set()
        if self.state.phase in ('thinking', 'speaking', 'tool', 'recording', 'transcribing'):
            if self.state.owner_alive():
                self.phase, self.status = 'interrupted', 'Another window is working · use /resume when it finishes'
            else:
                self.transcript.append(('Notice', 'The previous interaction was interrupted when the terminal closed. Any partial reply above is incomplete.'))
                self.phase, self.status = 'interrupted', 'Previous interaction interrupted · ready to continue'
                self._dirty = True
        self.active_id = 0
        self.fleet_generation = 0
        self.fleet_turn = None
        self.bug_turn = None
        self.bug_generation = 0
        self.ticket_turn = None
        self.ticket_generation = 0
        self.diagnostics_cancelled = None
        self.diagnostics_generation = 0
        self.fleet_watch = FleetWatch(self.state.path.parent / 'fleet' / (hashlib.sha256(str(self.cwd).encode()).hexdigest()[:20]+'.json'))
        self.scroll = self.state.scroll
        self.running = True
        self.key_protocol = False
        self.decoder = Decoder()
        self.ptt_held = False
        self.recording = None
        self.pending_recording = None
        self.capture_generation = 0
        self.mic_turn = None
        self.glyph_graphics = os.environ.get('TERM_PROGRAM', '').lower() in ('ghostty', 'kitty') or bool(os.environ.get('KITTY_WINDOW_ID'))
        self.glyph_pairs = {}
        self._preview_key = None
        self._home_draft = (self.input, self.cursor)
        self._stream_notice = ''
        # The tray reopens where it was left. Its contents are whatever the
        # background fetches have already delivered; draw() never fetches.
        self.tray_open, self.tray_view = self.state.tray_open, self.state.tray_view
        # Rule 2: a remembered good read outlives the window it was made in.
        self.tray_data = {'last_good': {name: dict(record)
                                        for name, record in self.state.tray_last_good.items()}}
        # Rule 1: the voice channel can be down while the workspace is unmuted.
        # This is never written back into settings.muted, or the service coming
        # back would leave Eric silently muted.
        self.voice_failure = ''
        # Rule 3: a conflict is held state, restated on the footer every frame.
        self.conflict = None
        # Rule 4: red once, then it fades to the plain status row.
        self._alert_until = None
        self._empty_capture_seen = False
        # Rule 5: one line per dead stream, not one per poll.
        self._stream_errors_seen = {}
        try:
            self.workstreams = WorkstreamManager(
                self.state.path.parent / 'workstreams' / (self.state.path.stem+'.json'),
                lambda events: Client(url, Settings(**dict(vars(self.settings), muted=True)), events),
                workspace_context={'cwd': str(self.cwd)[:512]})
        except (OSError, ValueError) as error:
            self.workstreams = DisabledWorkstreams(safe_text(error, 250))
            self.status = 'Workstreams unavailable · '+safe_text(error, 180)
        if self.workstreams.selected:
            self.input = self.focused_stream()['draft']
            self.cursor = len(self.input)


    def focused_stream(self):
        return next((item for item in self.workstreams.records if item['id'] == self.workstreams.selected), None)

    def focus_stream(self, identifier=None):
        if self.workstreams.selected:
            self.workstreams.set_draft(self.input)
        else:
            self._home_draft = (self.input, self.cursor)
        record = self.workstreams.focus(identifier)
        self.input, self.cursor = (record['draft'], len(record['draft'])) if record else self._home_draft
        self.history_index = None
        self.scroll = 0
        self._stream_notice = ''
        self.status = 'Focused '+(record['title'] if record else 'Home')
        self.mark_dirty()

    def set_tray(self, view=None, toggle=False):
        """Open the tray, or toggle it. Pure state: the fetch, if any, is the caller's."""
        if view is not None and view not in TRAY_VIEWS:
            raise ValueError('Tray views: '+', '.join(TRAY_VIEWS))
        self.tray_open = not self.tray_open if toggle and view in (None, self.tray_view) else True
        if view is not None:
            self.tray_view = view
        self.mark_dirty()

    def tray_status(self):
        return 'Tray · '+TRAY_TITLES[self.tray_view] if self.tray_open else 'Tray closed'

    def note_conflict(self, reason):
        """Rule 3: never overwrite, never silently replay. Say where the work is kept."""
        self.conflict = {'reason': safe_text(reason, 300),
                         'snapshot': str(self.state.recovery_path),
                         'pid': os.getpid(), 'other_pid': self.state.owner_pid,
                         'other_alive': self.state.owner_alive(),
                         'session_id': self.client.session_id}
        self.tray_data['windows'] = self.conflict
        self.status = ('Another window is ahead · Enter held until /resume · transcript, draft, '
                       'settings and session id kept in '+self.conflict['snapshot'])
        self.mark_dirty()

    def clear_conflict(self):
        self.conflict = None
        self.tray_data.pop('windows', None)

    def alert_once(self, message, repeat=False):
        """Rule 4: say it in red once; a repeat goes straight to the status row."""
        self.status = message
        self._alert_until = None if repeat else time.monotonic()+NOTICE_FADE
        self.mark_dirty()

    def fade_alert(self):
        if self._alert_until is not None and time.monotonic() >= self._alert_until:
            self._alert_until = None
            self.mark_dirty()

    def mark_dirty(self):
        self._dirty = True

    def set_phase(self, phase, status=None):
        if phase != self.phase:
            self._phase_started = time.monotonic()
        self.phase = phase
        if status is not None:
            self.status = status
        self.mark_dirty()

    def save_state(self, force=False):
        now = time.monotonic()
        if not self._dirty or (not force and now-self._last_save < .75):
            return not self._dirty
        self.state.transcript = WorkspaceState.bounded_transcript(self.transcript)
        if hasattr(self, 'workstreams') and self.workstreams.selected:
            try:
                self.workstreams.set_draft(self.input)
            except (OSError, ValueError) as error:
                self.status = safe_text(error, 250)
                return False
            self.state.draft, self.state.cursor = self._home_draft
        else:
            self.state.draft = self.input[:8000]
            self.state.cursor = self.cursor
        self.state.scroll = self.scroll
        self.state.tray_open, self.state.tray_view = self.tray_open, self.tray_view
        history, budget = [], 64000
        for text in reversed(self.input_history[-100:]):
            text = safe_text(text, min(8000, budget))
            history.append(text)
            budget -= len(text)
            if budget <= 0:
                break
        self.state.history = history[::-1]
        self.state.settings = dict(vars(self.settings))
        self.state.context = self.context
        self.state.phase = self.phase
        self.state.session_id = self.client.session_id
        self._last_save = now
        previous_fingerprint = self.state.fingerprint
        try:
            self.state.save()
            self._dirty = False
            return True
        except StateConflict as error:
            self.note_conflict(error)
            if self.input and not self.input.startswith('/'):
                self._conflict_draft = (self.input, self.cursor)
            return False
        except OSError as error:
            self.status = 'Could not save conversation: ' + safe_text(error, 150)
            return self.state.fingerprint != previous_fingerprint

    def manage_task(self, action):
        ident = action.get('action_id', '')
        if isinstance(ident,str) and ident in self._task_actions_seen:
            return
        try:
            if not isinstance(ident,str) or not re.fullmatch('[0-9a-f]{32}',ident):
                raise ValueError('Invalid task action identifier')
            self._task_actions_seen.add(ident)
            if action.get('error'):
                raise ValueError(safe_text(action['error'],250))
            if self.state.changed():
                raise ValueError('Another window saved newer context. Use /resume; task change was not applied.')
            self.state.task_store.reconcile(self.tasks)
            original_store, original_tasks = self.state.task_store, list(self.tasks)
            trial = TaskStore([dict(item) for item in original_store.records])
            receipt = trial.apply_change(action.get('change'))
            if receipt['changed']:
                self.state.task_store = trial
                self.tasks[:] = trial.legacy()
                self.mark_dirty()
                if not self.save_state(force=True):
                    self.state.task_store = original_store
                    self.tasks[:] = original_tasks
                    raise ValueError('Task change could not be saved. Use /resume if another window is active.')
            message = receipt['message']
        except (ValueError, TypeError, OSError) as error:
            message = 'Task change failed: '+safe_text(error, 250)
        self.state.task_receipts = (self.state.task_receipts+[message])[-4:]
        self.transcript.append(('Task manager', message))
        self.mark_dirty()
        self.save_state(force=True)

    def resume(self):
        """Explicitly adopt the latest conversation without discarding this draft."""
        draft, cursor = self._conflict_draft or (self.input, self.cursor)
        self.stop()
        latest = WorkspaceState(self.cwd, self.state.path)
        if latest.phase in ('thinking', 'speaking', 'tool', 'recording', 'transcribing') and latest.owner_alive():
            self.input, self.cursor = draft, cursor
            self.note_conflict('The other window is still working. Wait for it to finish, then /resume.')
            return False
        self.state = latest
        self.context = latest.context
        self.client.session_id = latest.session_id
        self.transcript = latest.transcript or [('Codsworth', 'At your service.')]
        self.tasks = latest.tasks
        if latest.panels:
            panel = latest.panels.get('today', next(iter(latest.panels.values())))
            self.panel = panel['title'], panel['content']
        for name, value in latest.settings.items():
            setattr(self.settings, name, value)
        self.input = draft or latest.draft
        self.cursor = cursor if draft else latest.cursor
        self.input_history = latest.history
        self.history_index = None
        self.scroll = latest.scroll
        self._conflict_draft = None
        self.clear_conflict()
        self.set_phase('ready', 'Resumed latest conversation · your draft is preserved')
        if latest.phase in ('thinking', 'speaking', 'tool', 'recording', 'transcribing'):
            self.transcript.append(('Notice', 'The saved reply was interrupted; its partial response is incomplete.'))
        self.save_state(force=True)
        return True

    def transcript_text(self):
        return '\n\n'.join(f'{role}\n{text}' for role, text in self.transcript) + '\n'

    def switch_conversation(self, ident):
        if self.state.changed() or (self.state.owner_alive() and self.state.loaded_phase in ('thinking', 'speaking', 'tool', 'recording', 'transcribing')):
            raise ValueError('Another window has newer or active context. Use /resume before switching.')
        snapshot = self.state.conversations.load(ident)
        if snapshot['conversation_id'] == self.state.conversation_id:
            self.status = 'Already in '+self.state.title
            return
        self.stop()
        self.mark_dirty()
        self.save_state(force=True)
        archived = WorkspaceState(self.cwd, self.state.conversations.directory / (snapshot['conversation_id']+'.json'))
        self.state.conversation_id, self.state.title = archived.conversation_id, archived.title
        self.context = archived.context
        task_panel = self.state.panels.get('tasks')
        self.state.panels = archived.panels
        if task_panel:
            self.state.panels['tasks'] = task_panel
        panel = self.state.panels.get('today') or self.state.panels.get('notes')
        self.panel = (panel['title'], panel['content']) if panel else ('Today', 'What shall we work on?')
        self.client.session_id = archived.session_id
        self.transcript = archived.transcript or [('Codsworth', 'At your service.')]
        self.input, self.cursor = archived.draft, archived.cursor
        self.input_history = archived.history
        self.history_index = None
        self.scroll = archived.scroll
        for name, value in archived.settings.items():
            setattr(self.settings, name, value)
        self._conflict_draft = None
        self.clear_conflict()
        self.set_phase('ready', 'Resumed '+self.state.title)
        if archived.phase in ('thinking', 'speaking', 'tool', 'recording', 'transcribing'):
            self.transcript.append(('Notice', 'The saved reply was interrupted; its partial response is incomplete.'))
        self.save_state(force=True)

    def update_panel(self, name, title, content):
        panel = dict(title=safe_text(title, 80), content=safe_text(content, 2400))
        self.panel = panel['title'], panel['content']
        self.transcript.append(self.panel)
        self.scroll = 0
        self.state.panels[name] = panel
        self.mark_dirty()
        self.save_state(force=True)

    def captured_tasks(self):
        return '\n'.join(f'{index}. {task}' for index, task in enumerate(self.tasks, 1))

    def poll_fleet_watch(self):
        if self.fleet_turn or time.monotonic() < self.fleet_watch.next_poll:
            return
        self.fleet_watch.next_poll = time.monotonic()+30
        if self.fleet_watch.enabled:
            updates = getattr(self, '_watch_updates_next', False)
            self._watch_updates_next = not updates
            self.refresh_fleet(background=True, updates=updates)

    def refresh_fleet(self, questions_only=False, updates=False, background=False):
        if self.fleet_turn:
            self.fleet_turn.cancel()
        self.fleet_generation += 1
        generation = self.fleet_generation
        turn = Turn(-3)
        self.fleet_turn = turn
        self._fleet_background = background
        if not background:
            self.status = 'Refreshing Fleet questions…' if questions_only else 'Refreshing Fleet…'
        def fetch():
            try:
                received = False
                path = '/api/fleet/updates' if updates else '/api/fleet/overview'
                payload = {'since': self.fleet_watch.since(), 'limit': 100} if updates else {}
                expected = 'fleet_updates' if updates else 'fleet_snapshot'
                for snapshot in self.client.stream(path, payload, turn):
                    if not isinstance(snapshot, dict) or snapshot.get('type') != expected:
                        raise ValueError('Unexpected Fleet response; check the service version')
                    received = True
                    if not turn.cancelled.is_set():
                        self.events.put((-3, dict(type=expected, token=generation,
                            snapshot=snapshot, questions_only=questions_only, background=background)))
                if not received and not turn.cancelled.is_set():
                    raise ValueError('Fleet returned no snapshot')
            except Exception as error:
                if not turn.cancelled.is_set():
                    self.events.put((-3, dict(type='fleet_error', token=generation,
                        message=self.client.hop_detail('Fleet via Codsworth', self.client.address(), 1, error),
                        background=background)))
        threading.Thread(target=fetch, daemon=True).start()

    def diagnose(self):
        if self.diagnostics_cancelled:
            self.diagnostics_cancelled.set()
        self.diagnostics_generation += 1
        generation = self.diagnostics_generation
        cancelled = threading.Event()
        self.diagnostics_cancelled = cancelled
        self.status = 'Checking services and local audio tools…'
        url = self.client.parsed.geturl()
        def check():
            result = collect_diagnostics(url, cancelled)
            if not cancelled.is_set():
                self.events.put((-5, dict(token=generation, checks=result)))
        threading.Thread(target=check, daemon=True).start()

    def refresh_tickets(self, value, maintenance=False, inbox=False):
        if inbox and value.strip():
            raise ValueError('Use /inbox without arguments; /issues filters Corner issues.')
        parts = value.split()
        payload = {'repo': 'example/codsworth'} if maintenance else {}
        command = '/bugs' if maintenance else '/issues'
        label = 'Corner work inbox' if inbox else ('Codsworth maintenance' if maintenance else 'Corner issues')
        path = '/api/inbox' if inbox else '/api/tickets'
        expected = 'work_inbox' if inbox else 'ticket_inbox'
        if parts:
            if parts[0] in ('open', 'closed', 'all'):
                if len(parts) > 2:
                    raise ValueError('Use /tickets [open|closed|all] [page], or /tickets NUMBER')
                payload['state'] = parts[0]
                if len(parts) == 2:
                    if not parts[1].isascii() or not parts[1].isdigit() or not 1 <= int(parts[1]) <= 10000:
                        raise ValueError('Ticket page must be 1–10000')
                    payload['page'] = int(parts[1])
            elif len(parts) == 1 and parts[0].isascii() and parts[0].isdigit() and 1 <= int(parts[0]) <= 2147483647:
                payload['issue'] = int(parts[0])
            else:
                raise ValueError('Use /tickets [open|closed|all] [page], or /tickets NUMBER')
        if self.ticket_turn:
            self.ticket_turn.cancel()
        self.ticket_generation += 1
        generation = self.ticket_generation
        turn = self.ticket_turn = Turn(-6)
        self.status = 'Loading '+label+'…'
        def fetch():
            try:
                received = False
                for result in self.client.stream(path, payload, turn):
                    if not isinstance(result, dict) or result.get('type') != expected:
                        raise ValueError('Unexpected ticket response')
                    received = True
                    if not turn.cancelled.is_set():
                        self.events.put((-6, dict(token=generation, result=result, command=command, label=label)))
                if not received and not turn.cancelled.is_set():
                    raise ValueError('No ticket response')
            except Exception:
                if not turn.cancelled.is_set():
                    self.events.put((-6, dict(token=generation, result=dict(ok=False,
                        error=self.client.hop_detail('Codsworth service', self.client.address(), 1,
                            'Could not load '+label+'. Try '+command+' again.')),
                        command=command, label=label)))
        threading.Thread(target=fetch, daemon=True).start()

    def file_bug(self, description):
        if not description.strip() or len(description) > 4000:
            raise ValueError('Use /bug followed by 1–4,000 characters describing what happened')
        if self.bug_turn:
            raise ValueError('A bug report is already being filed; wait for its receipt')
        self.bug_generation += 1
        generation = self.bug_generation
        turn = Turn(-4)
        self.bug_turn = turn
        self.status = 'Filing Codsworth bug…'
        def submit():
            try:
                received = False
                for result in self.client.stream('/api/bugs', {'description': description}, turn):
                    if not isinstance(result, dict) or result.get('type') != 'bug_report':
                        raise ValueError('Unexpected bug-report response')
                    received = True
                    if not turn.cancelled.is_set():
                        self.events.put((-4, dict(token=generation, result=result)))
                if not received and not turn.cancelled.is_set():
                    raise ValueError('Bug-report response was empty')
            except Exception:
                if not turn.cancelled.is_set():
                    self.events.put((-4, dict(token=generation, result={
                        'ok': False, 'outcome': 'unknown',
                        'error': self.client.hop_detail('Codsworth service', self.client.address(), 1,
                            'No receipt received. Check Codsworth tickets before retrying; the issue may already exist.')})))
        threading.Thread(target=submit, daemon=True).start()

    def command(self, text):
        previous_panel = self.panel
        previous_count = len(self.transcript)
        try:
            result = setting_command(text, self.settings)
            if result:
                self.status = result
                if text == '/mute' and self.settings.muted and self.client.active:
                    self.client.active.close_io(voice_only=True)
                return
            name, _, value = text.partition(' ')
            if self.workstreams.selected and name not in ('/stream','/streams','/focus','/tray','/windows','/quit','/exit'):
                self.focus_stream(None)
            if name in ('/quit', '/exit'):
                self.running = False
            elif name == '/streams':
                self.set_tray('streams', toggle=True)
                self.status = self.tray_status()
            elif name == '/windows':
                self.set_tray('windows', toggle=True)
                self.status = self.tray_status()
            elif name == '/tray':
                view = value.strip() or None
                self.set_tray(view, toggle=True)
                self.status = self.tray_status()
            elif name == '/focus':
                self.focus_stream(None if value.strip() in ('home', '') else value.strip())
            elif name == '/stream':
                operation, _, argument = value.partition(' ')
                if operation == 'new' and argument.strip():
                    # Save the old target's draft before create changes focus.
                    if self.workstreams.selected:
                        self.workstreams.set_draft(self.input)
                    else:
                        self._home_draft = (self.input, self.cursor)
                    record = self.workstreams.create(argument.strip())
                    self.input, self.cursor, self.scroll = '', 0, 0
                    self.status = 'Focused '+record['title']+' · send its first instruction'
                elif operation == 'stop' and argument.strip():
                    self.workstreams.stop(argument.strip())
                    self.status = 'Stopped selected workstream'
                else:
                    raise ValueError('Use /stream new TITLE or /stream stop ID; /streams lists IDs')
            elif name == '/help':
                self.panel = ('Controls', HELP)
            elif name == '/new':
                if self.state.changed() and not self.resume():
                    return
                self.stop()
                self.mark_dirty()
                self.save_state(force=True)
                self.state.conversation_id = uuid.uuid4().hex
                self.state.title = 'New conversation'
                self.context = None
                self.state.panels = {key: value for key, value in self.state.panels.items() if key == 'tasks'}
                self.panel = ('Today', 'What shall we work on?')
                self.client.session_id = ''
                self.state.session_id = ''
                self.transcript = [('Codsworth', 'A fresh page, then. How may I help?')]
                self.input = ''
                self.input_history = []
                self.history_index = None
                self.scroll = 0
                self.set_phase('ready', 'Ready · new conversation')
            elif name == '/sessions':
                self.mark_dirty()
                self.save_state(force=True)
                entries = self.state.conversations.entries()
                self.panel = ('Conversations', '\n'.join(
                    ('→ ' if item['conversation_id'] == self.state.conversation_id else '  ')+
                    item['conversation_id'][:8]+'  '+safe_text(item.get('title', 'Untitled'), 80)
                    for item in entries) or 'No saved conversations yet.')
                self.transcript.append(self.panel)
                self.scroll = 0
            elif name == '/switch':
                self.switch_conversation(value.strip())
            elif name == '/rename':
                if not value.strip():
                    self.edit_input('/rename '+self.state.title,len('/rename '+self.state.title))
                    self.status = 'Edit the title · Enter saves'
                else:
                    self.state.title = safe_text(value.strip(), 80).replace('\n', ' ')
                    self.status = 'Conversation renamed to '+self.state.title
            elif name == '/color':
                self.color_menu = True
                self._color_choice = self.settings.color
                self.speed_slider = False
                self.effort_menu = False
                self.status = 'Choose a conversation color'
            elif name == '/effort':
                self.effort_menu = True
                self._effort_choice = self.settings.effort
                self.speed_slider = False
                self.color_menu = False
                self.status = 'Choose reasoning effort'
            elif name == '/tasks':
                self.set_tray('tasks')
                self.state.task_store.reconcile(self.tasks)
                self.panel = ('Tasks', self.state.task_store.listing(value.strip() or 'open'))
                self.transcript.append(self.panel)
                self.scroll = 0
            elif name == '/task':
                self.state.task_store.reconcile(self.tasks)
                result = self.state.task_store.command(safe_text(value, 2000))
                self.tasks[:] = self.state.task_store.legacy()
                self.update_panel('tasks', 'Workspace tasks', self.state.task_store.listing('all'))
                self.status = result
            elif name == '/resume':
                self.resume()
            elif name in ('/fleet', '/questions'):
                self.set_tray('fleet')
                self.refresh_fleet(questions_only=name == '/questions')
            elif name == '/updates':
                self.refresh_fleet(updates=True)
            elif name == '/inbox':
                self.set_tray('inbox')
                self.refresh_tickets(value, inbox=True)
            elif name in ('/tickets', '/issues', '/bugs'):
                self.set_tray('inbox')
                self.refresh_tickets(value, maintenance=name == '/bugs')
            elif name == '/bug':
                self.file_bug(value)
            elif name == '/doctor':
                self.set_tray('doctor')
                self.diagnose()
            elif name == '/settings':
                self.panel = ('Settings', '\n'.join([
                    f'Speech: {"muted" if self.settings.muted else "on"} · /mute toggles',
                    f'Speed: {self.settings.speed:g}× · /speed opens the slider',
                    f'Voice: {self.settings.voice} · /voice expanded or /voice pilot',
                    f'Effort: {self.settings.effort} · /effort none low medium high',
                    f'Humor: {self.settings.humor:g} · /humor 0..1',
                    f'Sarcasm: {self.settings.sarcasm:g} · /sarcasm 0..1',
                    f'Motion: {self.settings.motion} · /motion full or /motion reduced',
                    f'Color: {self.settings.color} · /color opens the picker',
                    f'Fleet watch: {"on" if self.fleet_watch.enabled else "off"} · /watch on or off',
                    'Delivery settings are saved with this conversation; Fleet watch belongs to the workspace.']))
            elif name in ('/joke', '/tea', '/vault'):
                self.panel = ('Codsworth · aside', {'/joke': joke, '/tea': tea, '/vault': vault}[name]())
                self.expression = 'pleased'
            elif name == '/watch':
                if value not in ('on', 'off', ''):
                    raise ValueError('Use /watch on or /watch off')
                if value:
                    self.fleet_watch.enable(value == 'on')
                    if value == 'off' and self.fleet_turn and getattr(self, '_fleet_background', False):
                        self.fleet_turn.cancel()
                        self.fleet_turn = None
                        self.fleet_generation += 1
                self.status = 'Fleet watch '+('on · checks every 30 seconds' if self.fleet_watch.enabled else 'off')
            elif name == '/copy':
                if not shutil.which('pbcopy'):
                    raise ValueError('Clipboard unavailable. Use /export /path/to/conversation.txt')
                subprocess.run(['pbcopy'], input=self.transcript_text(), text=True, check=True, timeout=3)
                self.status = 'Conversation copied to clipboard'
            elif name == '/export':
                if not value.strip():
                    raise ValueError('Use /export /path/to/conversation.txt (creates a new file)')
                path = Path(value.strip()).expanduser()
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    stream.write(self.transcript_text())
                self.status = 'Conversation exported to ' + safe_text(path, 200)
            elif name == '/todo':
                if not value.strip():
                    raise ValueError('Use /todo followed by a task')
                if len(self.tasks) >= 100:
                    raise ValueError('This workspace has 100 active tasks. Archive completed tasks with /task archive ID first.')
                self.tasks.append(safe_text(value, 500))
                self.update_panel('tasks', 'Captured tasks', self.captured_tasks())
            elif name == '/todos':
                self.panel = ('Captured tasks', self.captured_tasks() or 'No tasks yet. /todo adds one.')
            elif name == '/done':
                try:
                    index = int(value) - 1
                except ValueError:
                    raise ValueError('Use /done followed by the task number') from None
                if not 0 <= index < len(self.tasks):
                    raise ValueError('No captured task with that number. /todos lists tasks.')
                if not self.tasks[index].startswith('[done] '):
                    self.tasks[index] = '[done] ' + self.tasks[index]
                self.update_panel('tasks', 'Captured tasks', self.captured_tasks())
            elif name == '/files':
                self.panel = ('Workspace · read only', '\n'.join(safe_text(p.name, 150) + ('/' if p.is_dir() else '')
                    for p in sorted(self.cwd.iterdir(), key=lambda p: p.name.lower())[:150]))
            elif name == '/links':
                links = extract_links(self.transcript)
                self._link_snapshot = ((self.state.conversation_id, self.client.session_id), tuple(links))
                self.panel = ('Links', '\n'.join(f'{i}. {url}' for i,url in enumerate(links,1)) or 'No web links in this conversation yet.')
            elif name == '/open':
                snapshot = getattr(self, '_link_snapshot', None)
                if not snapshot or snapshot[0] != (self.state.conversation_id, self.client.session_id):
                    raise ValueError('Run /links in this conversation before opening a link.')
                try:
                    selected = int(value.strip())
                except ValueError:
                    raise ValueError('Use /open followed by a number from /links.') from None
                if not 1 <= selected <= len(snapshot[1]):
                    raise ValueError('No link with that number. /links lists available URLs.')
                url = snapshot[1][selected-1]
                if not valid_url(url):
                    raise ValueError('This link is not a supported HTTP(S) URL.')
                opener = shutil.which('open' if sys.platform == 'darwin' else 'xdg-open')
                if not opener:
                    raise ValueError('No system browser opener is installed; copy the URL from /links.')
                subprocess.run([opener,url],check=True,timeout=10,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                self.status = 'Opened '+url
            elif name == '/plugins':
                self.panel = ('Tools and integrations', 'Available through Hermes: Fleet status and controls, Gitea issues (create, read, update, comment and labels), web search/extraction, terminal commands, files, task planning, memory and session search.\n\nCommands run in the Codsworth container (CT42201), not on your Mac.\n\nIntegration stubs: remote server/SSH, calendar, mail and external tasks. These require configuration before use.')
            else:
                raise ValueError('Unknown command. /help lists controls.')
            if (self.panel != previous_panel or name in ('/help','/todos','/files','/plugins','/links')) and len(self.transcript) == previous_count:
                self.transcript.append(self.panel)
                self.scroll = 0
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            self.set_phase('error', safe_text(error, 250))
        finally:
            self.mark_dirty()
            self.save_state(force=True)

    def send(self):
        draft = self.input
        command = attachments.is_command(draft)
        display = draft.strip()
        images = []
        if display and not command:
            try:
                text, images = attachments.prepare_message(draft, self.cwd)
            except ValueError as error:
                self.status = safe_text(error, 250)
                self.mark_dirty()
                return
        else:
            text = display
        if self.workstreams.selected and text and not command:
            try:
                self.workstreams.send(text, images=images or None)
                self._stream_notice = ''
                self.input, self.cursor, self.scroll = '', 0, 0
                self.workstreams.set_draft('')
                self._streams_open = False
                self.mark_dirty()
            except (ValueError, OSError) as error:
                self.status = safe_text(error, 250)
                self._stream_notice = self.status
            return

        if display and not command:
            if self.conflict or self.state.changed() or (self.state.phase in ('thinking', 'speaking', 'tool', 'recording', 'transcribing') and self.state.owner_alive()):
                # Rule 3: Enter is held, not silently replayed into the older window.
                self._conflict_draft = (self.input, self.cursor)
                self.mark_dirty()
                self.save_state(force=True)
                if not self.conflict:
                    self.note_conflict('The other window saved newer context.')
                return
        self.input = ''
        self.mark_dirty()
        if display and (not self.input_history or self.input_history[-1] != display):
            self.input_history.append(display)
            self.input_history = self.input_history[-100:]
        self.history_index = None
        if not text:
            return
        if command:
            self.command(text)
            return
        self.stop()
        self.transcript.extend([('You', display), ('Codsworth', '')])
        if self.state.title == 'New conversation':
            self.state.title = safe_text(display.splitlines()[0], 80)
        self.transcript = self.transcript[-100:]
        self.expression, self.status, self.scroll = 'thinking', 'Thinking…', 0
        self._turn_started = time.monotonic()
        self.set_phase('thinking')
        self._waiting_word = waiting_word(text)
        self._reply_pending = True
        self._silent_since = time.monotonic()
        panels = {key: dict(value) for key, value in self.state.panels.items()}
        if self.tasks:
            previous = panels.get('tasks', {}).get('content', '')
            panels['tasks'] = dict(title='Tasks', content=safe_text(
                'Captured tasks:\n' + self.captured_tasks() + '\n\nLast task panel:\n' + previous, 2400))
        self.client.workspace_context = dict(cwd=str(self.cwd)[:512], panels=panels)
        # Keep canonical task IDs in model context; never substitute a prose plan.
        panel_budget = 3000
        for panel in panels.values():
            panel['content'] = panel['content'][:max(0,panel_budget)]
            panel_budget -= len(panel['content'])
        self.state.task_store.reconcile(self.tasks)
        records, record_budget = [], 2800
        for record in self.state.task_store.active_records():
            size = len(json.dumps(record,ensure_ascii=False))
            if size > record_budget:
                continue
            records.append(dict(record)); record_budget -= size
        self.client.workspace_context.update(task_records=records, task_count=len(self.state.task_store.active_records()),
            task_receipts=[safe_text(item,200) for item in self.state.task_receipts[-4:]])
        local_now = datetime.now().astimezone()
        self.client.workspace_context.update(local_date=local_now.date().isoformat(),
            timezone=local_now.strftime('%Z (%z)'))
        self._task_actions_seen.clear()
        self.active_id = self.client.submit(text, images=images) if images else self.client.submit(text)
        self.save_state(force=True)

    def cancel_capture(self):
        self.capture_generation += 1
        if self.recording:
            recording, self.recording = self.recording, None
            recording.cancel()
            def cleanup():
                try:
                    recording.finish()
                except Exception:
                    pass
            threading.Thread(target=cleanup, daemon=True).start()
        if self.mic_turn:
            self.mic_turn.cancel()
            self.mic_turn = None
        if self.pending_recording:
            self.pending_recording.cancel()

    def begin_capture(self):
        self.stop()
        try:
            self.recording = Recording(device=os.environ.get('CODS_MIC_DEVICE', '0'))
            self.status = 'Listening · hold Space while speaking; release to send (60 s max)'
            self.expression = 'neutral'
            self.set_phase('recording')
        except Exception as error:
            self.set_phase('error', safe_text(error, 600))
            self._alert_until = time.monotonic()+NOTICE_FADE

    def end_capture(self):
        if not self.recording:
            return
        recording, self.recording = self.recording, None
        self.pending_recording = recording
        token = self.capture_generation
        turn = Turn(-token)
        self.mic_turn = turn
        self.status, self.expression = 'Transcribing your speech… · Esc cancels', 'thinking'
        self.set_phase('transcribing')
        def transcribe():
            try:
                data = recording.finish()
                if data is None or turn.cancelled.is_set():
                    return
                text = ''
                for event in self.client.stream('/api/transcribe', data, turn, kind='voice',
                                                content_type='application/octet-stream'):
                    text = safe_text(event.get('text', ''), 8000).strip()
                if not turn.cancelled.is_set():
                    self.events.put((-2, dict(type='transcribed', token=token, text=text)))
            except Exception as error:
                if not turn.cancelled.is_set():
                    self.events.put((-2, dict(type='microphone_error', token=token, message=safe_text(error, 600))))
            finally:
                if self.mic_turn is turn:
                    self.mic_turn = None
                if self.pending_recording is recording:
                    self.pending_recording = None
        threading.Thread(target=transcribe, daemon=True).start()

    def stop(self):
        if hasattr(self, 'workstreams') and self.workstreams.selected and self.running:
            self.cancel_capture()
            try:
                self.workstreams.stop()
                self.status = 'Workstream interrupted · other work continues'
            except (ValueError, OSError) as error:
                self.status = safe_text(error, 250)
            return
        sidecar_active = bool(self.diagnostics_cancelled or self.bug_turn or self.fleet_turn or self.ticket_turn)
        if self.ticket_turn:
            self.ticket_turn.cancel()
            self.ticket_turn = None
            self.ticket_generation += 1
        if self.diagnostics_cancelled:
            self.diagnostics_cancelled.set()
            self.diagnostics_cancelled = None
            self.diagnostics_generation += 1
        if self.bug_turn:
            self.bug_turn.cancel()
            self.bug_turn = None
            self.bug_generation += 1
            self.transcript.append(('Bug report', 'Stopped waiting for the report receipt. The issue may already exist; check Codsworth tickets before retrying.'))
            self.mark_dirty()
        if self.fleet_turn:
            self.fleet_turn.cancel()
            self.fleet_turn = None
            self.fleet_generation += 1
        was_active = sidecar_active or self.phase in ('thinking', 'speaking', 'tool', 'recording', 'transcribing') or self._reply_pending
        if self._reply_pending:
            self.transcript.append(('Notice', 'Reply interrupted. Any partial response above is incomplete.'))
        self._reply_pending = False
        self._turn_started = None
        self._silent_since = None
        self.cancel_capture()
        self.client.interrupt()
        self.active_id = 0
        self.expression = 'neutral'
        if was_active:
            self.set_phase('interrupted', 'Interrupted · ready for your next thought')
        self.save_state(force=True)

    def record_last_good(self, inbox):
        """Rule 2: remember what a good read held, so a later failure can say."""
        good = self.tray_data.setdefault('last_good', {})
        available = inbox.get('available', {}) if isinstance(inbox, dict) else {}
        stamp = safe_text(str(inbox.get('observed_at', '')), 40)
        for key in ('issues', 'pulls'):
            if available.get(key):
                records = inbox.get(key) or []
                good[key] = {'at': stamp, 'count': len(records),
                             'records': [dict(item) for item in records[:8] if isinstance(item, dict)]}
        if available.get('fleet') and isinstance(inbox.get('fleet'), dict):
            good['fleet'] = {'at': stamp, 'count': len(inbox['fleet'].get('questions') or []), 'records': []}
        self.remember_last_good()

    def remember_last_good(self):
        """Only the time and the size are persisted; the rows are this window's."""
        self.state.tray_last_good = {name: {'at': record['at'], 'count': record['count']}
                                     for name, record in self.tray_data.get('last_good', {}).items()
                                     if name in LAST_GOOD_SECTIONS}
        self.mark_dirty()

    def report_stream_failures(self):
        """Rule 5: one red line in Home per dead stream; nothing else is disturbed."""
        live = set()
        for record in self.workstreams.records:
            live.add(record['id'])
            summary = safe_text(record.get('summary', ''), 250) if record.get('status') == 'error' else ''
            if not summary:
                self._stream_errors_seen.pop(record['id'], None)
                continue
            if self._stream_errors_seen.get(record['id']) == summary:
                continue
            self._stream_errors_seen[record['id']] = summary
            # A background failure never steals focus, speech or the phase: it is
            # one line, and the stream's own transcript stays readable via /focus.
            self.transcript.append(('Error', record['id'][:8]+' '+safe_text(record['title'], 40)
                + ' stopped · '+summary+' · other streams continue · /focus '
                + record['id'][:8]+' reads its transcript'))
            self.transcript = self.transcript[-100:]
            self.mark_dirty()
        for ident in [key for key in self._stream_errors_seen if key not in live]:
            self._stream_errors_seen.pop(ident, None)

    def drain(self):
        if hasattr(self, 'workstreams'):
            try:
                if self.workstreams.poll():
                    self._draw_key = None
            except (ValueError, OSError) as error:
                self.status = safe_text(error, 250)
            self.report_stream_failures()
        while True:
            try:
                local_id, event = self.events.get_nowait()
            except queue.Empty:
                self.check_stall()
                self.fade_alert()
                self.save_state()
                return
            if local_id == -6:
                if event['token'] != self.ticket_generation:
                    continue
                self.ticket_turn = None
                result = event['result']
                command = event.get('command', '/issues')
                label = event.get('label', 'Corner issues')
                if result.get('type') == 'work_inbox':
                    self.record_last_good(result)
                    self.tray_data['inbox'] = result
                    content = inbox_text(result)
                    self.status = 'Corner inbox refreshed' if not result.get('errors') else 'Corner inbox · some sources unavailable'
                elif result.get('ok') is True:
                    lines = []
                    for ticket in result.get('tickets', [])[:20]:
                        lines.append(f"#{ticket['number']}  [{ticket['state']}]  {ticket['title']}\n{ticket['url']}")
                        if 'body' in ticket:
                            lines.append(ticket['body'])
                            if ticket.get('body_truncated'):
                                lines.append('Description abbreviated; open the ticket for the full text.')
                    if result.get('possibly_more'):
                        page = result.get('page', 1)
                        lines.append(f"Next page: {command} {result.get('state', 'open')} {page+1}" if page < 10000 else 'Page limit reached; use Gitea to browse further.')
                    content = '\n\n'.join(lines) or 'No matching issues in '+label+'.'
                    self.status = label+' loaded · '+command+' NUMBER for details · /links to open'
                else:
                    content = result.get('error', 'Could not load tickets.')
                    self.status = 'Ticket inbox unavailable'
                self.transcript.append((label, safe_text(content, 30000)))
                self.scroll = 0
                self.mark_dirty()
                self.save_state(force=True)
                continue
            if local_id == -5:
                if event['token'] != self.diagnostics_generation:
                    continue
                self.diagnostics_cancelled = None
                self.tray_data['doctor'] = event['checks']
                self.transcript.append(('Diagnostics', format_checks(event['checks'])))
                self.status = 'Checks complete · /bug can report a problem you describe'
                self.mark_dirty()
                self.save_state(force=True)
                continue
            if local_id == -4:
                if event['token'] != self.bug_generation:
                    continue
                self.bug_turn = None
                result = event['result']
                if result.get('ok'):
                    receipt = f"Filed Codsworth bug #{result.get('number', '?')}: {result.get('url', '')}"
                else:
                    receipt = 'Bug report: '+safe_text(result.get('error', 'Filing failed'), 600)
                    if result.get('outcome') == 'unknown' and 'before retrying' not in receipt:
                        receipt += '\nCheck Codsworth tickets before retrying; the issue may already exist.'
                self.transcript.append(('Bug report', safe_text(receipt, 1000)))
                self.status = 'Bug filed' if result.get('ok') else 'Bug report needs attention'
                self.mark_dirty()
                self.save_state(force=True)
                continue
            if local_id == -3:
                if event['token'] != self.fleet_generation:
                    continue
                self.fleet_turn = None
                background = event.get('background', False)
                if event['type'] == 'fleet_error':
                    self.tray_data['fleet'] = None
                    self.tray_data['fleet_error'] = event['message']
                    if background:
                        if self.phase in ('ready', 'interrupted'):
                            self.status = 'Fleet watch could not refresh · retrying in 30 seconds'
                        continue
                    self.transcript.append(('Fleet', 'Could not refresh Fleet: '+event['message']+'\nTry /fleet again.'))
                    self.status = 'Fleet unavailable · text chat remains available'
                elif event['type'] == 'fleet_updates':
                    snapshot = event['snapshot']
                    try:
                        events = self.fleet_watch.consume_updates(snapshot)
                        if background and not events:
                            if snapshot.get('errors') and self.phase in ('ready', 'interrupted'):
                                self.status = 'Fleet updates unavailable · position preserved'
                            continue
                        self.transcript.append(('Fleet updates', safe_text(updates_text(snapshot, events), 24000)))
                        if not background:
                            self.status = 'Fleet updates checked'
                    except OSError:
                        self.status = 'Could not save Fleet update position; try again'
                else:
                    snapshot = event['snapshot']
                    self.tray_data['fleet'] = snapshot
                    self.tray_data.pop('fleet_error', None)
                    if snapshot.get('available', {}).get('questions'):
                        self.tray_data.setdefault('last_good', {})['fleet'] = {
                            'at': safe_text(str(snapshot.get('observed_at', '')), 40),
                            'count': len(snapshot.get('questions') or []), 'records': []}
                        self.remember_last_good()
                    if background:
                        if snapshot.get('errors'):
                            if self.phase in ('ready', 'interrupted'):
                                self.status = 'Fleet watch partially unavailable · retrying in 30 seconds'
                            continue
                        try:
                            changed = self.fleet_watch.observe(snapshot)
                        except OSError:
                            self.status = 'Could not save Fleet watch state'
                            continue
                        if not changed:
                            continue
                        # Codsworth opens the tray himself only for work that
                        # blocks Eric, and never while he is mid-draft.
                        if fleet_questions(snapshot) and not self.input:
                            self.set_tray('fleet')
                            self.transcript.append(('Notice', '◇ tray  opened by Codsworth · fleet'))
                        counts = snapshot.get('counts', {})
                        self.transcript.append(('Fleet watch',
                            f"{'Baseline' if changed == 'baseline' else 'Changed'}: {counts.get('running', '?')} running, "
                            f"{counts.get('queued', '?')} queued, {len(snapshot.get('questions', []))} questions. /fleet for details."))
                        self.mark_dirty()
                        self.save_state(force=True)
                        continue
                    self.transcript.append(('Fleet questions' if event['questions_only'] else 'Fleet',
                        safe_text(overview_text(snapshot, event['questions_only']), 24000)))
                    self.status = 'Fleet refreshed' if not snapshot.get('errors') else 'Fleet partially refreshed'
                self.mark_dirty()
                self.save_state(force=True)
                continue
            if local_id == -2:
                if event['token'] != self.capture_generation:
                    continue
                self.expression = 'neutral'
                if event['type'] == 'microphone_error':
                    self.set_phase('error', event['message'])
                    self._alert_until = time.monotonic()+NOTICE_FADE
                elif not event['text']:
                    # Red once. A second empty capture in a row is a status row,
                    # not another alarm.
                    self.set_phase('ready')
                    self.alert_once('No speech recognized. Hold Space while speaking, then release.',
                                    repeat=self._empty_capture_seen)
                    self._empty_capture_seen = True
                elif self.input:
                    self._empty_capture_seen = False
                    self.transcript.append(('Recognized speech', event['text']))
                    self.set_phase('ready', 'Speech shown above; kept your typed draft unchanged')
                else:
                    self._empty_capture_seen = False
                    self.input = event['text']
                    self.send()
                continue
            if local_id == self.active_id and self._reply_pending:
                self._silent_since = time.monotonic()
            if local_id != self.active_id:
                continue
            kind = event['type']
            if kind == 'session':
                try:
                    self.state.session_id = uuid.UUID(event['session_id']).hex
                    self.client.session_id = self.state.session_id
                    self.mark_dirty()
                    self.save_state(force=True)
                except (ValueError, OSError):
                    self.status = 'Could not persist this conversation session'
                self.backend_label = safe_text(' · '.join(filter(None, [event.get('runtime'), event.get('model')])), 80) or 'Conversation service'
            elif kind == 'text':
                index = next((i for i in range(len(self.transcript)-1,-1,-1) if self.transcript[i][0]=='Codsworth'), None)
                if index is not None:
                    label, text = self.transcript[index]
                    self.transcript[index] = (label, (text + event['delta'])[-30000:])
                    self.mark_dirty()
            elif kind == 'tool':
                tool = event.get('tool', event)
                self.set_phase('tool' if tool.get('state') == 'running' else 'thinking',
                    safe_text(tool.get('summary', 'Working…'), 160))
            elif kind == 'action':
                action = event['action']
                if action.get('name') == 'manage_task':
                    self.manage_task(action)
                elif action.get('name') == 'expression' and action.get('expression') in ('neutral', 'pleased', 'skeptical', 'concerned', 'annoyed'):
                    self.expression = action['expression']
                elif action.get('name') == 'show_panel' and action.get('panel_id') in ('today', 'tasks', 'notes'):
                    self.update_panel(action['panel_id'], action.get('title', 'Notes'), action.get('content', ''))
            elif kind == 'speaking':
                # The service is answering again, so the service-muted state lifts
                # itself. Nothing here ever consults or writes settings.muted.
                self.voice_failure = ''
                if self.phase != 'error':
                    self.set_phase('speaking', 'Speaking · Esc to interrupt')
            elif kind == 'notice' and event.get('channel') == 'voice':
                # Rule 1: the reply is already on screen. A dead optional channel
                # marks the chip red and says why; it never fails the turn, and it
                # must not touch settings.muted or the service returning would
                # leave Eric silently muted.
                message = safe_text(event['message'], 600)
                self.voice_failure = message
                self.transcript.append(('Notice', message))
                self.alert_once(message)
            elif kind in ('error', 'notice'):
                self.set_phase('error', safe_text(event['message'], 600))
                self.transcript.append(('Error' if kind == 'error' else 'Notice', self.status))
                if kind == 'error':
                    if self._reply_pending:
                        self.transcript.append(('Notice', 'The reply did not finish. Any partial response above is incomplete.'))
                        self._reply_pending = False
            elif kind == 'context':
                self.context = context_usage(event.get('used'), event.get('limit'))
                if self.context and self.context[2] >= CONTEXT_WARNING:
                    self.status = (f'Conversation is {int(self.context[2]*100)}% of the model context · '
                        '/new starts a fresh one before it overflows')
                self.mark_dirty()
            elif kind == 'complete':
                if self._reply_pending and self.phase != 'error' and self._turn_started is not None:
                    self.transcript.append(('Worked for', work_duration(time.monotonic()-self._turn_started)))
                self._turn_started = None
                self._reply_pending = False
                if self.phase != 'error':
                    self.set_phase('ready', 'Ready')
                self.expression = 'neutral'
                self.mark_dirty()
                self.save_state(force=True)

    def check_stall(self):
        """Report an upstream turn that has gone quiet; a busy session never streams."""
        started = self._silent_since
        if started is None or not self._reply_pending:
            return
        waited = time.monotonic()-started
        if waited >= STALL_LIMIT:
            self._silent_since = None
            self._reply_pending = False
            self.stop()
            self.transcript.append(('Error', self.client.hop_detail('Hermes gateway', self.client.address(), 1,
                f'No reply for {int(waited)} seconds. The Hermes session may still be busy '
                'or compacting an over-long conversation; /new starts a fresh one.')))
            self.set_phase('error', 'Hermes went quiet · /new starts a fresh conversation')
            self.mark_dirty()
        elif waited >= STALL_NOTICE and not self.status.startswith('Waiting for Hermes'):
            self.status = (f'Waiting for Hermes at {self.client.address()} · quiet for {int(waited)}s'
                           ' · Esc to interrupt · /new for a fresh session')
            self.mark_dirty()

    def mode_label(self):
        """Speech output mode shown in the footer and cycled with Shift+Tab."""
        return 'muted' if self.settings.muted else 'voice on'

    def cycle_mode(self):
        self.command('/mute')
        self.status = self.status+' · Shift+Tab cycles'
        self.mark_dirty()

    def composer_target(self):
        """The label on the composer rule: it names where Enter goes, nothing more."""
        focused = self.focused_stream()
        if focused:
            return f"{focused['id'][:8]} {safe_text(focused['title'], 40)}"
        return 'Home'

    def interaction_state(self):
        """Operational state is independent of Codsworth's facial expression."""
        return {'recording': 'LISTENING', 'tool': 'THINKING'}.get(self.phase, self.phase.upper())

    def completion_options(self, refresh=False):
        if self.input.startswith('/stream ') and ' ' not in self.input[len('/stream '):]:
            return [(name, description) for name,description in (('/stream new','Create a workstream'),('/stream stop','Stop one workstream')) if name.startswith(self.input)]
        for prefix in ('/focus ', '/stream stop '):
            if self.input.startswith(prefix):
                query = self.input[len(prefix):].casefold()
                choices = [(prefix+'home', 'Main Codsworth conversation')] if prefix=='/focus ' and 'home'.startswith(query) else []
                choices.extend((prefix+r['id'], r['title']+' · '+r['status']) for r in self.workstreams.records
                    if r['id'].startswith(query) or query in r['title'].casefold())
                return choices
        sessions=[]
        if self.input.startswith('/switch '):
            # Cache while editing a prefix; reload on a new completion session or title change.
            key=(self.state.path,self.state.conversation_id,self.state.title)
            if getattr(self,'_session_completion_key',None) != key or (refresh and time.monotonic()-getattr(self,'_session_completion_at',0)>1):
                self._session_choices=session_choices(self.state.conversations.directory)
                self._session_completion_key=key
                self._session_completion_at=time.monotonic()
            sessions=self._session_choices
        else:
            self._session_completion_key=None
        return command_matches(self.input,sessions,self.state.task_store.records)

    def draw(self, screen):
        import textwrap
        height, width = screen.getmaxyx()
        color_menu = getattr(self,'color_menu',False)
        session_color = self._color_choice if color_menu else self.settings.color
        if getattr(self,'_accent_pair_color',None) != session_color and getattr(curses,'COLORS',0):
            colors = {'green':(114,curses.COLOR_GREEN),'cyan':(80,curses.COLOR_CYAN),
                      'amber':(222,curses.COLOR_YELLOW),'blue':(111,curses.COLOR_BLUE),
                      'purple':(183,curses.COLOR_MAGENTA),'white':(252,curses.COLOR_WHITE)}
            foreground = colors[session_color][0 if curses.COLORS>=256 else 1]
            curses.init_pair(4,foreground,-1)
            curses.init_pair(7,foreground,234 if curses.COLORS>=256 else -1)
            self._accent_pair_color = session_color
        bright, soft, border, accent, failure = (curses.color_pair(i) for i in (1, 2, 3, 4, 8))
        frame_attr = accent
        def put(y, x, text, attr=None, limit=None):
            if 0 <= y < height and 0 <= x < width:
                try:
                    screen.addnstr(y, x, safe_text(text).replace('\n', ' '),
                        max(0, min(width-x-1, limit) if limit is not None else width-x-1),
                        bright if attr is None else attr)
                except curses.error:
                    pass
        user_accent = curses.color_pair(5)
        focused = self.focused_stream()
        shown_transcript = focused['transcript'] if focused else self.transcript
        state = ('THINKING' if focused['status'] in ('running','thinking','tool') else 'READY') if focused else self.interaction_state()
        working = state in ('THINKING', 'TRANSCRIBING')
        active_message = None
        if working:
            for index in range(len(shown_transcript)-1,-1,-1):
                label = shown_transcript[index][0]
                if label in ('You','Worked for'):
                    break
                if label == 'Codsworth':
                    active_message = index
                    break
            if active_message is None:
                active_message = len(shown_transcript)
                shown_transcript = [*shown_transcript,('Codsworth','')]
        active_animation = state in ('LISTENING','THINKING','TRANSCRIBING','SPEAKING')
        reduced = getattr(self.settings, 'motion', 'full') == 'reduced'
        frame = (int(time.monotonic() / (.32 if reduced else .08)) % 32) if active_animation else (0 if reduced else int(time.monotonic()*2)%32)
        now = time.monotonic()
        elapsed = int(now-getattr(self, '_phase_started', now)) if state in ('THINKING', 'SPEAKING', 'LISTENING', 'TRANSCRIBING') else None
        if state == 'THINKING':
            started = (self.workstreams._started.get(focused['id']) if focused
                       else self._turn_started)
            if started is not None:
                elapsed = max(0, int(now-started))
        # Reduced motion holds the first suggestion; rotation is motion the user opted out of.
        slot = 0 if (reduced or self.input) else int(now/SUGGESTION_SECONDS)
        # The spectrum is part of the frame, so it must invalidate the frame.
        levels = getattr(getattr(self, 'client', None), 'levels', None)
        spectrum = levels.window(now, max(0, width-4)) if levels is not None and state == 'SPEAKING' else None
        # The tray is a pure render of state fetched elsewhere, and it is part of
        # the frame: without it in the key the tray goes stale.
        view = self.tray_view if self.tray_view in TRAY_VIEWS else 'glance'
        tray = tuple(tray_content(view, self.tray_data, self.workstreams.records,
                                  self.workstreams.selected, self.state.task_store))
        badges = tuple(tray_badges(self.tray_data, len(open_tasks(self.state.task_store))))
        open_width = tray_width(view, width)
        draw_key = (height, width, session_color,color_menu,tuple((label,text) for label,text in shown_transcript),
            tuple((r['id'],r['status'],r.get('unread')) for r in self.workstreams.records),
            self.workstreams.selected, tuple(sorted(self._expanded_messages)), self._selection_start, self._selection_end, self.input, self.cursor, self.scroll, self.status, self.expression, self.settings.speed, self.settings.muted, self.settings.voice, self.settings.effort, frame, state, elapsed, slot, self.context, self.key_protocol, getattr(self, '_waiting_word', ''), str(self.cwd), self.state.title, self.state.conversation_id, bool(self.recording), getattr(self, 'speed_slider', False), getattr(self, 'effort_menu', False), getattr(self, '_command_index', 0), spectrum, self.tray_open, view, tray, badges,
            bool(self.voice_failure), self.conflict['snapshot'] if self.conflict else '',
            bool(self._alert_until and now < self._alert_until))
        if getattr(self, '_draw_key', None) == draw_key:
            return
        self._draw_key = draw_key
        screen.erase()
        self._message_hits = {}
        self._message_cells = {}
        if height < 16 or width < 60:
            # Too small for the tray, but never too small to say what is waiting.
            put(0, 1, 'Codsworth · enlarge to 60 × 16')
            badge_x = width-1-sum(len(text)+1 for text, _ in badges)
            for text, role in badges:
                put(0, badge_x, text, failure if role == 'alert' else (accent if role == 'accent' else soft), 3)
                badge_x += len(text)+1
            put(height-2, 1, '› '+self.input, bright)
            put(height-1, 1, 'Enter:send | ^T:tray', soft)
            screen.refresh()
            if self.glyph_graphics:
                self._glyph_key = None
                self._preview_key = None
                sys.stdout.write(kitty_clear()+kitty_clear_previews()); sys.stdout.flush()
            return
        draft_lines, cursor_row, cursor_col = composer_layout(self.input, self.cursor, width-10)
        preview_cards = [attachments.preview_card(path)
                         for _, _, path in attachments.resolved_references(self.input, self.cwd)]
        shown_rows, composer_top, divider, preview_rows, meta_rows, composer_padding = composer_metrics(
            height, len(draft_lines), len(preview_cards), self.glyph_graphics)
        draft_offset = max(0, cursor_row-shown_rows+1)
        # Shut, the tray is a two-column pull tab and the transcript keeps the full
        # width; open, the contents choose 26 columns or 39 (2a). The sprite sits at
        # the foot of the open tray and is not drawn at all when the tray is shut.
        tray_columns = open_width if self.tray_open else 0
        rule_column = width-tray_columns-2
        tray_limit = max(0, width-rule_column-3)
        sprite_columns = sprite_rows = sprite_top = sprite_x = 0
        if tray_columns:
            wrapped = []
            for text, role in tray:
                wrapped.extend((piece, role) for piece in (textwrap.wrap(text, max(8, tray_limit)) if text else ['']))
            room = min(12, max(0, divider-3-len(wrapped)-1))
            if room >= 4:
                sprite_columns, sprite_rows = fit_cells(19, room)
                sprite_top, sprite_x = divider-sprite_rows, rule_column+2
        # The transcript stops short of the tray's rule, or of the shut spine.
        text_width = max(20, (rule_column if tray_columns else (width-5 if open_width else width-3))-4)
        self._transcript_right = 3+text_width
        # Row 0: identity and speech mode.
        # Rule 1: a dead voice service is its own red state with its own reason.
        # It is never the workspace mute setting, which stays exactly as Eric left it.
        chip = ('☎ voice off · muted' if self.settings.muted
                else '◌ voice off · service' if self.voice_failure
                else f'◐ voice on · {self.settings.speed:g}×')
        chip_x = width-3-len(chip)
        # Rule 4: print the form that works in this terminal, never a key that
        # cannot arrive. Alt is erased without the protocol and Escape lands first.
        hint = ('⌥\\ tray' if self.key_protocol else '^T tray')
        hint_x = chip_x-3-len(hint)
        # At 60 columns the tray cannot open, so its badges move up beside the
        # title and blocked work still shows (2f).
        header_badges = badges if not open_width else ()
        badge_x = hint_x-1-sum(len(text)+1 for text, _ in header_badges)
        put(0, 1, 'Codsworth', accent | curses.A_BOLD)
        # The workspace path is Mac context; Codsworth's own shell runs inside Cods.
        workspace = str(self.cwd).replace(str(Path.home()), '~', 1)
        title = safe_text(self.state.title or '', 80).replace('\n', ' ').strip()
        room = max(0, (badge_x if header_badges else hint_x)-12)
        header = '· '+workspace+(' · '+title if title else '')
        if len(header) > room:
            excess = len(header)-room
            workspace = '…'+workspace[excess+1:] if len(workspace) > excess+1 else '…'
            header = '· '+workspace+(' · '+title if title else '')
        put(0, 11, header, soft, room)
        if chip_x > 12:
            put(0, chip_x, chip, failure if self.settings.muted or self.voice_failure else accent)
        if hint_x > 12:
            put(0, hint_x, hint, soft)
        if header_badges and badge_x > 12:
            for text, role in header_badges:
                put(0, badge_x, text, failure if role == 'alert' else (accent if role == 'accent' else soft))
                badge_x += len(text)+1
        put(1, 1, '─'*(width-2), border)
        activity = 'listening' if state == 'LISTENING' else ('thinking' if state in ('THINKING','TRANSCRIBING') else
                     ('talking' if state == 'SPEAKING' else 'idle'))
        if not self.glyph_graphics and sprite_columns:
            for row,cells in enumerate(render_cells(self.expression,activity,frame,sprite_columns,sprite_rows,background=(0,0,0))):
                for column,(character,foreground,background) in enumerate(cells):
                    pair=self.glyph_pairs.get((foreground,background))
                    if pair is None and getattr(curses,'COLORS',0)>=256 and len(self.glyph_pairs)+16<curses.COLOR_PAIRS:
                        pair=len(self.glyph_pairs)+16
                        curses.init_pair(pair,foreground,background)
                        self.glyph_pairs[foreground,background]=pair
                    put(sprite_top+row,sprite_x+column,character,curses.color_pair(pair or 1))
        top = 2
        lines=[]
        message_ranges = []
        user_rows = set()
        reply_group = False
        active_heading = None
        for message_index,(label,text) in enumerate(shown_transcript):
            if label == 'Worked for':
                reply_group = False
                caption = '── Worked for '+text+' '
                lines.append(('', soft, '', soft, [], 0))
                lines.append(((caption+'─'*text_width)[:text_width-2], soft, '', soft, [], 0))
                continue
            user=label.lower()=='you'
            conversation = user or label == 'Codsworth'
            failed = label in ERROR_LABELS
            tool = label == 'Tool'
            secondary = reply_group and not user and not failed and label not in ('Codsworth', 'Notice')
            nested = tool or secondary
            speaker_attr = (failure if failed else curses.color_pair(12) if user else user_accent if tool or secondary
                            else accent)
            rail_attr = speaker_attr if user or failed else border
            if label == 'Codsworth':
                reply_group = True
            elif user or failed or label == 'Notice':
                reply_group = False
            # Conversation text uses a prompt/dot; only supporting blocks have titles.
            if lines:
                lines.append(('', soft, '', soft, [], 0))
            block_start = len(lines)
            if user:
                lines.append(('', bright, '', speaker_attr, [], 0))
            message_start = len(lines)
            if message_index == active_message:
                active_heading = message_start
                lines.append(('',soft,'●',accent,[],0))
            elif not conversation:
                lines.append((label,speaker_attr | curses.A_BOLD,
                              '└' if nested else '●',speaker_attr,[],2 if nested else 0))
            body_color = user_accent if tool else bright
            roles = ({'body': failure, 'heading': failure | curses.A_BOLD, 'code': failure, 'muted': failure} if failed
                     else {'body': body_color, 'heading': body_color | curses.A_BOLD, 'code': body_color, 'muted': soft})
            roles.update(strong=(failure if failed else curses.color_pair(11)) | curses.A_BOLD,
                         inline_code=failure if failed else user_accent)
            roles['code'] = failure if failed else user_accent
            roles['heading'] = speaker_attr | curses.A_BOLD
            roles['metric'] = accent
            message_key = (self.state.conversation_id, self.workstreams.selected or '', message_index)
            body_indent = 0 if conversation else 4 if nested else 2
            rendered = render_message(text,text_width-2-body_indent,styled=True,code_indent=4)
            if message_index == active_message and not text:
                rendered = []
            expanded = message_key in self._expanded_messages
            collapsible = len(rendered) > 18
            shown = rendered if expanded or not collapsible else rendered[:10]
            lines.extend((line, roles[role],
                          ('›' if user else '●') if conversation and index == 0 and message_index != active_message
                          else '' if conversation else '└' if role == 'code' and (index+1 == len(shown) or shown[index+1][1] != 'code')
                          else '│', speaker_attr if conversation else rail_attr,
                          [(part, roles[style]) for part,style in (highlight_metrics(spans) if secondary else spans)],
                          body_indent+(4 if role == 'code' else 2 if role == 'muted' and line else 0))
                         for index,(line,role,spans) in enumerate(shown))
            if collapsible and not expanded:
                lines.append((f'… {len(rendered)-10} more lines · click to expand', soft, '', soft, [], body_indent))
            if user:
                lines.append(('', bright, '', speaker_attr, [], 0))
                user_rows.update(range(block_start,len(lines)))
            message_ranges.append((block_start,len(lines),message_key if collapsible else None))
        options = self.completion_options() if not (getattr(self, 'speed_slider', False) or color_menu or getattr(self,'effort_menu',False)) else []
        picker_cap = 8 if height >= 36 else 4 if height >= 24 else 2
        picker_rows = min(len(options), picker_cap, max(0, divider-top-3))
        picker_frame = picker_rows > 0 and divider-picker_rows-1 >= 2
        settings_menu_rows = 6 if getattr(self, 'speed_slider', False) or color_menu or getattr(self,'effort_menu',False) else 0
        # No workstream bar: the streams live in the tray (slice 2). Its row is
        # the transcript's now.
        capacity=max(1,divider-top-picker_rows-int(picker_frame)-settings_menu_rows-(1 if height >= 24 else 0))
        previous_count = getattr(self, '_line_count', len(lines))
        if self.scroll and getattr(self, '_line_width', width) == width and len(lines) > previous_count:
            self.scroll += len(lines)-previous_count
        self._line_count, self._line_width = len(lines), width
        self.scroll=min(self.scroll,max(0,len(lines)-capacity))
        end=max(capacity,len(lines)-self.scroll)
        visible=lines[max(0,end-capacity):end]
        # The newest message sits just above the composer; a short conversation
        # does not float at the top of an empty screen.
        base=top+max(0,capacity-len(visible))
        activity_label = 'Transcribing' if state == 'TRANSCRIBING' else 'Working'
        activity_visible = False
        def shimmer(y,x):
            for offset,level in enumerate(shimmer_levels(activity_label,frame,reduced)):
                put(y,x+offset,activity_label[offset],
                    (soft,bright,curses.color_pair(11))[level] | curses.A_BOLD,1)
        tinted = {curses.color_pair(a): curses.color_pair(b)
                  for a,b in ((1,13),(11,14),(5,15),(2,9))}
        def on_user_background(style):
            weight = style & curses.A_BOLD
            return tinted.get(style & ~curses.A_BOLD, style & ~curses.A_BOLD) | weight
        for row,(line,attr,rail,rail_attr,spans,indent) in enumerate(visible):
            absolute_row = max(0,end-capacity)+row
            if absolute_row in user_rows:
                # Paint the whole message row, including wrapped lines and gaps
                # around inline formatting, without tinting the reply or tray.
                put(base+row,3,' '*text_width,curses.color_pair(13),text_width)
                attr = on_user_background(attr)
                spans = [(part,on_user_background(style)) for part,style in spans]
            for first,last,message in message_ranges:
                if first <= absolute_row < last:
                    self._message_hits[base+row] = message
                    break
            put(base+row,3+indent,rail,rail_attr,1)
            put(base+row,5+indent,line,attr,text_width-2-indent)
            self._message_cells[base+row] = " "*indent+line if line else ""
            column = 5+indent
            for part,part_attr in spans:
                if part_attr != attr:
                    put(base+row,column,part,part_attr,max(0,text_width+3-column))
                column += sum(cell_width(char) for char in part)
            if absolute_row == active_heading:
                activity_visible = True
                put(base+row,5,f'{activity_label} ({work_duration(elapsed)})',soft,text_width-2)
                shimmer(base+row,5)
        input_text, input_marker, input_hint, input_error = (curses.color_pair(i) for i in (6, 7, 9, 10))
        if picker_rows:
            selected = min(getattr(self, '_command_index', 0), len(options)-1)
            start = max(0,selected-picker_rows+1)
            visible_options = options[start:start+picker_rows]
            name_width = min(16, max((len(name) for name,_ in visible_options), default=8))
            inner = max(0, width-6)
            if picker_frame:
                put(divider-picker_rows-1,2,'╭'+'─'*inner+'╮',frame_attr)
            for row,(name,description) in enumerate(visible_options):
                active = start+row == selected
                y = divider-picker_rows+row
                attr = (input_marker | curses.A_REVERSE | curses.A_BOLD) if active else input_hint
                put(y,2,'│',frame_attr)
                put(y,3,' '*inner,attr,inner)
                put(y,2+inner+1,'│',frame_attr)
                label = name if len(name) <= name_width else name[:name_width-1]+'…'
                put(y,4,f'{label:<{name_width}}  {description}',attr,max(0,inner-2))
        if color_menu or getattr(self,'effort_menu',False):
            menu_top = divider-settings_menu_rows
            put(menu_top,3,'─'*text_width,accent,text_width)
            title = 'Conversation color' if color_menu else 'Reasoning effort'
            put(menu_top+1,5,title,bright | curses.A_BOLD,text_width-2)
            x = 5
            choices = COLOR_OPTIONS if color_menu else EFFORT_LEVELS
            current = self._color_choice if color_menu else getattr(self,'_effort_choice',self.settings.effort)
            for item in choices:
                selected = item == current
                attr = (accent | curses.A_REVERSE | curses.A_BOLD) if selected else soft
                put(menu_top+3,x,' '+item+' ',attr,len(item)+2)
                if selected:
                    put(menu_top+4,x+1+len(item)//2,'▲',accent,1)
                x += len(item)+3
        elif settings_menu_rows:
            menu_top = divider-settings_menu_rows
            scale_width = min(54,text_width-10)
            scale_x = 5+max(0,(text_width-4-scale_width)//2)
            put(menu_top,3,'─'*text_width,accent,text_width)
            put(menu_top+1,5,'Speech speed',bright | curses.A_BOLD,text_width-2)
            put(menu_top+1,19,f'{self.settings.speed:g}×',user_accent | curses.A_BOLD,text_width-16)
            put(menu_top+3,scale_x,'Slower',soft)
            put(menu_top+3,scale_x+scale_width-5,'Faster',soft)
            put(menu_top+4,scale_x,'─'*(scale_width+1),border,scale_width+1)
            selected_x = scale_x+round((self.settings.speed-.5)/1.5*scale_width)
            put(menu_top+4,selected_x,'▲',user_accent | curses.A_BOLD,1)
            for value in (.5,1,1.5,2):
                tick = f'{value:g}×'
                tick_x = scale_x+round((value-.5)/1.5*scale_width)
                put(menu_top+5,tick_x-len(tick)//2,tick,
                    user_accent | curses.A_BOLD if self.settings.speed == value else soft,len(tick))
        roles = {'title': accent | curses.A_BOLD, 'body': bright, 'soft': soft,
                 'accent': accent, 'alert': failure}
        if tray_columns:
            put(1,rule_column,'┬',border)
            for row in range(2,divider):
                put(row,rule_column,'│',border)
            put(2,rule_column+2,TRAY_TITLES[view],accent | curses.A_BOLD,tray_limit)
            body_rows = max(0,(sprite_top or divider)-3)
            shown = wrapped[:body_rows] if len(wrapped) <= body_rows else wrapped[:max(0,body_rows-1)]
            for row,(line,role) in enumerate(shown):
                put(3+row,rule_column+2,line,roles[role],tray_limit)
            if len(wrapped) > len(shown):
                put(3+len(shown),rule_column+2,'… more · /'+view,soft,tray_limit)
        elif open_width:
            # Shut: a dim spine and a two-column pull tab, nothing else (2b).
            for row in range(2,divider):
                put(row,width-5,'│',border)
            put(3,width-3,'⌥\\' if self.key_protocol else '^T',accent)
            put(4,width-3,'──',border)
            for index,(text,role) in enumerate(badges[:max(0,min(4,divider-5))]):
                put(5+index,width-3,text,failure if role == 'alert' else (accent if role == 'accent' else soft))
        # A continuous, visible frame separates the draft from the transcript.
        # Labels live inside the box (Grok-style); the top rule stays a clean edge
        # so the tray joint cannot collide with the context meter.
        rule_width = max(0, width-4)
        if picker_frame:
            put(divider,2,'├'+'─'*(rule_width-2)+'┤',frame_attr)
        else:
            put(divider,2,'╭'+'─'*(rule_width-2)+'╮',frame_attr)
        if tray_columns and 3 < rule_column < width-4:
            put(divider,rule_column,'┴' if not picker_frame else '│',border)
        inner_top = divider+1
        inner_bottom = height-2
        for y in range(inner_top, inner_bottom):
            put(y,3,' '*(width-6),input_text,width-6)
            put(y,2,'│',frame_attr)
            put(y,width-3,'│',frame_attr)
        preview_placements = []
        if preview_cards:
            preview_top = divider+1+composer_padding
            thumbs = self.glyph_graphics and preview_rows >= 4
            thumb_cols = 14 if len(preview_cards) == 1 else 8
            thumb_h = max(1, preview_rows-1)
            visible = preview_cards[:4 if thumbs else preview_rows]
            if thumbs:
                put(preview_top, 4, '▣', input_marker, 1)
                put(preview_top, 6, '  ·  '.join(attachments.preview_label(card) for card in visible),
                    input_hint, width-10)
                x = 4
                for index, card in enumerate(visible):
                    if x + thumb_cols >= width-4:
                        break
                    if not card.get('error'):
                        preview_placements.append((card['path'], thumb_cols, thumb_h,
                                                   preview_top+1, x, PREVIEW_IMAGE_IDS[index]))
                    x += thumb_cols + 2
            else:
                for index, card in enumerate(visible):
                    put(preview_top+index, 4, '▣', input_marker, 1)
                    put(preview_top+index, 6, attachments.preview_label(card), input_hint, width-10)
        put(composer_top,4,'›',
            (input_error if self.recording else input_marker) | curses.A_BOLD)
        for row,line in enumerate(draft_lines[draft_offset:draft_offset+shown_rows]):
            put(composer_top+row,6,line,input_text,width-10)
        if not self.input:
            put(composer_top,6,'Listening… release Space to send' if self.recording else suggestion(slot),
                input_error if self.recording else input_hint,width-10)
        warn = bool(self.context and self.context[2] >= CONTEXT_WARNING)
        meters = (context_label(self.context), f'ctx {int(self.context[2]*100)}%') if self.context else ('ctx —',)
        mode = ('muted' if self.settings.muted else 'voice off · /doctor' if self.voice_failure
                else f'{self.settings.speed:g}×')
        if self.settings.effort != 'none':
            mode = self.settings.effort+' · '+mode
        target = self.composer_target()
        greeting = ('At your service.' if state == 'READY' and self.status in ('Ready', '')
                    and not focused and not self.input else '')
        meta_y = composer_top+shown_rows if meta_rows else divider
        if meta_rows and greeting:
            put(meta_y, 4, greeting, input_hint, width-10)
        left = (6+len(greeting)+2) if (meta_rows and greeting) else 6
        kind, start, shown, meter, text = composer_meta(target, meters, mode, width, left, bool(meta_rows))
        if kind == 'inner':
            meter_attr = input_error if warn else input_hint
            put(meta_y, start, text, input_hint, len(text))
            if shown:
                put(meta_y, start, shown, input_marker | curses.A_BOLD, len(shown))
                put(meta_y, start+len(shown)+3, meter, meter_attr, len(meter))
            else:
                put(meta_y, start, meter, meter_attr, len(meter))
        else:
            meter_attr = failure if warn else soft
            origin = divider if kind == 'rule' else (meta_y if meta_rows else divider)
            label = shown if kind == 'rule' else target
            put(origin, 4, label, accent | curses.A_BOLD,
                len(label) if kind == 'rule' else max(4, width-6-len(meter)-1))
            put(origin, start, meter, meter_attr, len(meter))
        # Row height-2 is a plain rule except while real audio is playing.
        put(height-2,2,'╰'+'─'*(rule_width-2)+'╯',frame_attr)
        if spectrum:
            put(height-2,3,spectrum,accent,rule_width-2)
        tray_key = '⌥\\' if self.key_protocol else '^T'
        keys = footer_keys(self.input, self.settings.muted, tray_key, attachments.attachment_hint(self.input))
        trailing = '/help'
        limit = max(0, width-9-len(trailing))
        primary = ((self._stream_notice or focused['title']+' · '+focused['status']) if focused
                   else (greeting if greeting and not meta_rows else ('' if self.status in ('Ready', '') else self.status)))
        alerting = bool(self._alert_until and now < self._alert_until)
        attr, tail = (failure if state == 'ERROR' or alerting else soft), keys
        if state == 'LISTENING':
            primary, tail, attr = f'◉ recording {duration(elapsed)} · Esc discards', '', failure
        elif state == 'SPEAKING':
            primary, tail = f'◐ Speaking · {duration(elapsed)} · Esc to interrupt', ''
        elif state in ('THINKING', 'TRANSCRIBING'):
            primary, tail, attr = ('Esc stop' if activity_visible else f'{activity_label} ({work_duration(elapsed)}) · Esc stop'), '', soft
        if getattr(self, '_exit_armed_at', None) is not None and now-self._exit_armed_at <= 3:
            primary, tail, attr = 'Press Ctrl-C again to exit · any other key stays here', '', soft
        elif getattr(self, 'speed_slider', False) or color_menu or getattr(self,'effort_menu',False):
            primary, tail, attr = '←/→:adjust | Enter:confirm | Esc:cancel', '', soft
        elif options:
            primary, tail, attr = '↑↓:choose | Tab:complete | Enter:use', '', soft
        elif self.scroll:
            primary, tail, attr = ((f'{activity_label} ({work_duration(elapsed)}) · Esc stop' if working and not activity_visible
                                   else 'Earlier messages · PgDn scroll · Ctrl+PgDn latest'),
                                  'Esc stop' if working and activity_visible else '',soft)
        # Rule 3: the reason sits on the footer every frame, not once. Only the
        # momentary exit prompt outranks it, and that one expires by itself.
        if self.conflict and getattr(self, '_exit_armed_at', None) is None:
            primary, tail, attr = ('Another window is ahead · /resume adopts it · /windows shows where '
                                   'this window\'s work is kept: '+self.conflict['snapshot'], '', failure)
        if not primary:
            primary, tail = tail, ''
        put(height-1,3,primary,attr,limit)
        if working and not activity_visible and primary.startswith(activity_label+' ('):
            shimmer(height-1,3)
        if tail and len(primary)+3+len(tail) <= limit:
            put(height-1,3+len(primary),' | '+tail,soft)
        # /help keeps its place whatever the status says: the primary line is
        # clipped to `limit`, which already reserves the trailing hint's columns.
        # A long failure reason must never push the way out of the window.
        if limit and 3+min(len(primary),limit)+2 <= width-3-len(trailing):
            put(height-1,width-3-len(trailing),trailing,soft)
        self.paint_selection(put)
        try:
            screen.move(composer_top+cursor_row-draft_offset,6+cursor_col)
        except curses.error:
            pass
        screen.refresh()
        if self.glyph_graphics and sprite_columns:
            glyph_key = (self.expression,activity,frame,sprite_columns,sprite_rows,sprite_top,sprite_x)
            if getattr(self, '_glyph_key', None) != glyph_key:
                self._glyph_key = glyph_key
                sys.stdout.write(kitty_at(sprite_top, sprite_x,
                    kitty_frame(self.expression,activity,frame,sprite_columns,sprite_rows)))
                sys.stdout.flush()
        elif self.glyph_graphics and getattr(self, '_glyph_key', None) is not None:
            self._glyph_key = None
            sys.stdout.write(kitty_clear()); sys.stdout.flush()
        if self.glyph_graphics:
            preview_key = tuple((str(path), cols, rows, y, x, image_id)
                                for path, cols, rows, y, x, image_id in preview_placements)
            if getattr(self, '_preview_key', None) != preview_key:
                self._preview_key = preview_key
                parts = [kitty_clear_previews()]
                parts.extend(kitty_at(y, x, kitty_file(path, cols, rows, image_id))
                             for path, cols, rows, y, x, image_id in preview_placements)
                sys.stdout.write(''.join(parts)); sys.stdout.flush()

    def selected_text(self):
        if self._selection_start is None or self._selection_end is None:
            return ''
        (y1,x1),(y2,x2)=sorted((self._selection_start,self._selection_end))
        rows=[]
        for y in range(y1,y2+1):
            text=self._message_cells.get(y,'')
            lo=max(0,x1-5) if y==y1 else 0
            hi=max(0,x2-5+1) if y==y2 else 100000
            column=0;part=[]
            for char in text:
                cells=cell_width(char)
                if lo<=column<hi:part.append(char)
                column+=cells
            rows.append(''.join(part).rstrip())
        return '\n'.join(rows).strip()

    def paint_selection(self, put):
        if self._selection_start is None or self._selection_end is None:return
        (y1,x1),(y2,x2)=sorted((self._selection_start,self._selection_end))
        for y in range(y1,y2+1):
            column=5
            for char in self._message_cells.get(y,''):
                if (y>y1 or column>=x1) and (y<y2 or column<=x2):
                    put(y,column,char,curses.color_pair(1)|curses.A_REVERSE)
                column+=cell_width(char)

    def handle_key(self, event):
        if event.kind == 'mouse':
            if event.text in ('wheel_up','wheel_down'):
                self.scroll=max(0,self.scroll+(3 if event.text=='wheel_up' else -3))
                self._selection_start=self._selection_end=None
            elif event.text=='click':
                self._selection_start=self._selection_end=None
                if event.y in self._message_cells and 5<=event.x<getattr(self,'_transcript_right',0):
                    self._selection_start=(event.y,event.x)
            elif event.text=='drag' and self._selection_start is not None:
                self._selection_end=(event.y,event.x)
            elif event.text=='release' and self._selection_start is not None:
                if self._selection_end is not None:
                    selected=self.selected_text()
                    if selected:
                        payload=base64.b64encode(selected.encode()).decode()
                        sys.stdout.write('\x1b]52;c;'+payload+'\x07');sys.stdout.flush()
                        self.status='Copied selection'
                else:
                    key=self._message_hits.get(self._selection_start[0])
                    if key is not None:
                        if key in self._expanded_messages:self._expanded_messages.remove(key)
                        else:self._expanded_messages.add(key)
                    self._selection_start=None
            self.mark_dirty()
            return
        if event.kind == 'press' and event.text == '\t' and event.modifiers & 4:
            ids = [None]+[r['id'] for r in self.workstreams.records]
            self.focus_stream(ids[(ids.index(self.workstreams.selected)+1)%len(ids)])
            return
        if event.kind == 'press' and event.text == '\x03':
            self.color_menu = False
            now = time.monotonic()
            previous = getattr(self, '_exit_armed_at', None)
            if previous is not None and now-previous <= 3:
                self.running = False
                return
            if getattr(self, 'speed_slider', False):
                self.settings.speed = self._speed_original
                self.speed_slider = False
            self.effort_menu = False
            self.stop()
            self._exit_armed_at = now
            self.status = 'Interrupted · press Ctrl-C again within 3 seconds to exit'
            return
        if event.kind in ('press', 'paste') and event.text != '\x03':
            self._exit_armed_at = None
        if event.kind == 'repeat' and event.text == '\x03':
            return
        if event.kind == 'paste':
            self._command_index = 0
            # Insert the entire paste as one draft edit; newlines are never key events.
            text = ''.join(c for c in event.text if c in '\n\t' or c.isprintable()).replace('\t', '    ')
            dropped = attachments.normalize_drop(text)
            if dropped != text or attachments.image_references(dropped):
                text = dropped
                if self.cursor and not self.input[:self.cursor][-1:].isspace():
                    text = ' '+text
            room = max(0,8000-len(self.input))
            truncated = len(text) > room
            if truncated and attachments.image_references(text.lstrip()):
                self.status = 'Image paths do not fit in the 8,000-character draft; existing text kept.'
                return
            text = text[:room]
            before = len(attachments.image_references(self.input))
            self.edit_input(self.input[:self.cursor]+text+self.input[self.cursor:], self.cursor+len(text))
            if len(attachments.image_references(self.input)) > before:
                self.status = 'Image paths added'
            elif truncated:
                self.status = 'Paste shortened to fit the 8,000-character draft; existing text kept.'
            return
        if getattr(self,'color_menu',False) and event.kind in ('press','repeat'):
            key = event.text
            if key in ('left',curses.KEY_LEFT,'right',curses.KEY_RIGHT):
                delta = -1 if key in ('left',curses.KEY_LEFT) else 1
                self._color_choice = COLOR_OPTIONS[(COLOR_OPTIONS.index(self._color_choice)+delta)%len(COLOR_OPTIONS)]
            elif key in ('\r','\n',curses.KEY_ENTER):
                self.settings.color = self._color_choice
                self.color_menu = False
                self.status = 'Conversation color: '+self.settings.color
            elif key == '\x1b':
                self.color_menu = False
                self.status = 'Color unchanged'
            self.mark_dirty()
            self.save_state(force=True)
            return
        if getattr(self, 'effort_menu', False) and event.kind in ('press', 'repeat'):
            key = event.text
            current = getattr(self, '_effort_choice', self.settings.effort)
            if current not in EFFORT_LEVELS:
                current = 'none'
            if key in ('left',curses.KEY_LEFT,'right',curses.KEY_RIGHT):
                delta = -1 if key in ('left',curses.KEY_LEFT) else 1
                self._effort_choice = EFFORT_LEVELS[(EFFORT_LEVELS.index(current)+delta)%len(EFFORT_LEVELS)]
            elif key in ('\r','\n',curses.KEY_ENTER):
                self.settings.effort = current
                self.effort_menu = False
                self.status = 'Reasoning effort: '+self.settings.effort
            elif key == '\x1b':
                self.effort_menu = False
                self.status = 'Effort unchanged'
            self.mark_dirty()
            self.save_state(force=True)
            return
        if getattr(self, 'speed_slider', False) and event.kind in ('press', 'repeat'):
            key = event.text
            if key in ('left', curses.KEY_LEFT, 'right', curses.KEY_RIGHT):
                delta = -.1 if key in ('left', curses.KEY_LEFT) else .1
                self.settings.speed = max(.5,min(2,round(self.settings.speed+delta,1)))
            elif key in ('\r','\n',curses.KEY_ENTER):
                self.speed_slider = False
                self.status = f'Speed {self.settings.speed:g}× · applies to the next utterance'
            elif key in ('\x1b','\x03'):
                self.settings.speed = self._speed_original
                self.speed_slider = False
            if hasattr(self, 'mark_dirty'): self.mark_dirty()
            return
        if event.kind == 'protocol':
            self.key_protocol = event.code & 10 == 10
            return
        if event.kind == 'focus_lost':
            if self.recording or self.mic_turn:
                self.stop()
                self.status = 'Microphone cancelled when terminal lost focus'
            self.ptt_held = False
            return
        if event.kind == 'focus_gained':
            return
        key = event.text
        if key == '\t' and event.modifiers & 1:
            # Shift+Tab cycles speech output whether or not the command picker is open.
            if event.kind == 'press':
                self.cycle_mode()
            return
        options = self.completion_options(refresh=True)
        if options and event.kind in ('press','repeat'):
            if key in ('up',curses.KEY_UP,'down',curses.KEY_DOWN):
                direction = -1 if key in ('up',curses.KEY_UP) else 1
                self._command_index = (getattr(self,'_command_index',0)+direction) % len(options)
                return
            if key == '\t':
                name = options[min(getattr(self,'_command_index',0),len(options)-1)][0]
                self.edit_input(name,len(name))
                self._command_index = 0
                return
            if key in ('\r','\n',curses.KEY_ENTER) and not event.modifiers & 3:
                name = options[min(getattr(self,'_command_index',0),len(options)-1)][0]
                self.edit_input(name,len(name))
            elif key not in ('left',curses.KEY_LEFT,'right',curses.KEY_RIGHT):
                self._command_index = 0
        if event.code == 32 and event.modifiers & 0x3f == 0:
            if event.kind == 'release':
                if self.ptt_held:
                    self.end_capture()
                self.ptt_held = False
                return
            if self.ptt_held:
                return
            if event.kind == 'press' and not self.input and self.key_protocol:
                self.ptt_held = True
                self.begin_capture()
                return
        if event.kind == 'release':
            return
        if key in ('\x1b', '\x03'):
            self.stop()
        elif key == '\x04':
            self.edit_input(self.input[:self.cursor]+self.input[self.cursor+1:], self.cursor)
        elif key == '\x14':
            # Ctrl+T decodes identically with and without the protocol, so it is
            # the primary toggle. Both branches sit above the printable one, or
            # ⌥1 would simply type '1' into the draft.
            self.set_tray(toggle=True)
            self.status = self.tray_status()
        elif self.key_protocol and event.modifiers & 2 and event.code in TRAY_ACCELERATORS:
            # Alt accelerators only under the protocol: without it Alt is erased
            # and a bare Escape arrives first, which stops speech.
            self.set_tray(TRAY_ACCELERATORS[event.code], toggle=True)
            self.status = self.tray_status()
        elif self.recording:
            return
        elif key in ('\n', '\r', curses.KEY_ENTER):
            if event.modifiers & 3:
                self.edit_input(self.input[:self.cursor]+'\n'+self.input[self.cursor:], self.cursor+1)
            elif self.input.strip() == '/speed':
                self.input = ''
                self._speed_original = self.settings.speed
                self.speed_slider = True
                if hasattr(self, 'mark_dirty'): self.mark_dirty()
            else:
                self.send()
        elif key in ('left', curses.KEY_LEFT):
            self.cursor = max(0,self.cursor-1)
        elif key in ('right', curses.KEY_RIGHT):
            self.cursor = min(len(self.input),self.cursor+1)
        elif key in ('home', curses.KEY_HOME, '\x01'):
            self.cursor = self.input.rfind('\n',0,self.cursor)+1
        elif key in ('end', curses.KEY_END, '\x05'):
            end = self.input.find('\n',self.cursor)
            self.cursor = len(self.input) if end < 0 else end
        elif key in ('up', curses.KEY_UP, 'down', curses.KEY_DOWN):
            direction = -1 if key in ('up', curses.KEY_UP) else 1
            if '\n' in self.input and not event.modifiers & 2:
                start = self.input.rfind('\n',0,self.cursor)+1
                column = self.cursor-start
                if direction < 0 and start:
                    previous = self.input.rfind('\n',0,start-1)+1
                    self.cursor = min(previous+column,start-1)
                elif direction > 0:
                    end = self.input.find('\n',self.cursor)
                    if end >= 0:
                        following = self.input.find('\n',end+1)
                        self.cursor = min(end+1+column,following if following >= 0 else len(self.input))
            else:
                self.recall_input(direction)
        elif key in ('delete', curses.KEY_DC):
            self.edit_input(self.input[:self.cursor]+self.input[self.cursor+1:],self.cursor)
        elif key in ('\b', '\x7f', curses.KEY_BACKSPACE):
            if self.cursor:
                self.edit_input(self.input[:self.cursor-1]+self.input[self.cursor:],self.cursor-1)
        elif key == '\x15':
            self.edit_input(self.input[self.cursor:],0)
        elif key == '\x0b':
            self.edit_input(self.input[:self.cursor],self.cursor)
        elif key == '\x17':
            start = self.cursor
            while start and self.input[start-1].isspace(): start -= 1
            while start and not self.input[start-1].isspace(): start -= 1
            self.edit_input(self.input[:start]+self.input[self.cursor:],start)
        elif key in ('page_up', curses.KEY_PPAGE):
            self.scroll += 8
        elif key in ('page_down', curses.KEY_NPAGE):
            self.scroll = 0 if event.modifiers & 4 else max(0, self.scroll-8)
        elif isinstance(key, str) and key.isprintable() and len(self.input) < 8000:
            self.edit_input(self.input[:self.cursor]+key+self.input[self.cursor:],self.cursor+len(key))
        if hasattr(self, 'mark_dirty'): self.mark_dirty()

    def resize(self, screen):
        """Adopt the terminal's new size and repaint from a clean slate."""
        if hasattr(curses, 'update_lines_cols'):
            curses.update_lines_cols()
        self._draw_key = None
        if self.glyph_graphics and (getattr(self, '_glyph_key', None) is not None or getattr(self, '_preview_key', None)):
            self._glyph_key = None
            self._preview_key = None
            sys.stdout.write(kitty_clear()+kitty_clear_previews())
            sys.stdout.flush()
        screen.clear()
        self.draw(screen)

    def run(self, screen):
        curses.curs_set(1)
        curses.noecho()
        curses.raw()  # Ctrl-C becomes a key, so it interrupts rather than exiting.
        screen.keypad(True)
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        curses.mouseinterval(0)
        if hasattr(curses, 'set_escdelay'):
            curses.set_escdelay(25)
        screen.timeout(50)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            palette = (252, 244, 240, -1) if curses.COLORS >= 256 else (curses.COLOR_WHITE, curses.COLOR_WHITE, curses.COLOR_WHITE, -1)
            curses.init_pair(1, palette[0], palette[3])
            curses.init_pair(2, palette[1], palette[3])
            curses.init_pair(3, palette[2], palette[3])
            curses.init_pair(4, 114 if curses.COLORS >= 256 else curses.COLOR_GREEN, -1)
            curses.init_pair(5, 222 if curses.COLORS >= 256 else curses.COLOR_YELLOW, palette[3])
            input_background = 234 if curses.COLORS >= 256 else -1
            curses.init_pair(6, 151 if curses.COLORS >= 256 else palette[0], input_background)
            curses.init_pair(7, 114 if curses.COLORS >= 256 else curses.COLOR_GREEN, input_background)
            curses.init_pair(9, 243 if curses.COLORS >= 256 else palette[1], input_background)
            curses.init_pair(10, 203 if curses.COLORS >= 256 else curses.COLOR_RED, input_background)
            curses.init_pair(11, 255 if curses.COLORS >= 256 else curses.COLOR_WHITE, palette[3])
            curses.init_pair(12, 180 if curses.COLORS >= 256 else curses.COLOR_YELLOW, input_background)
            curses.init_pair(13, palette[0], input_background)
            curses.init_pair(14, 255 if curses.COLORS >= 256 else curses.COLOR_WHITE, input_background)
            curses.init_pair(15, 222 if curses.COLORS >= 256 else curses.COLOR_YELLOW, input_background)
            curses.init_pair(8, 203 if curses.COLORS >= 256 else curses.COLOR_RED, palette[3])
            screen.bkgd(' ', curses.color_pair(1))
        sys.stdout.write(KEYBOARD_ENABLE)
        sys.stdout.flush()
        try:
            while self.running:
                self.drain()
                self.poll_fleet_watch()
                if hasattr(self, 'save_state'): self.save_state()
                if self.recording and (self.recording.process.poll() is not None or time.monotonic()-self.recording.started >= 60):
                    self.end_capture()
                for event in self.decoder.expire():
                    self.handle_key(event)
                self.draw(screen)
                try:
                    key = screen.get_wch()
                except curses.error:
                    continue
                except KeyboardInterrupt:
                    self.handle_key(Key('\x03', code=3))
                    continue
                if key == curses.KEY_MOUSE:
                    try:
                        _,x,y,_,buttons = curses.getmouse()
                        action = ('wheel_up' if buttons & curses.BUTTON4_PRESSED else
                                  'wheel_down' if buttons & getattr(curses,'BUTTON5_PRESSED',0) else
                                  'click' if buttons & curses.BUTTON1_PRESSED else
                                  'release' if buttons & curses.BUTTON1_RELEASED else
                                  'drag' if buttons & curses.REPORT_MOUSE_POSITION and self._selection_start is not None else '')
                        if action:self.handle_key(Key(action,kind='mouse',x=x,y=y))
                    except curses.error:
                        pass
                    continue
                if key == curses.KEY_RESIZE:
                    # Without this curses keeps reporting the old geometry, so
                    # every row after a resize is drawn at the wrong place: the
                    # terminal scrolls, rules land mid-screen and fragments of
                    # the previous frame survive. Re-read the size, drop the
                    # memoised frame and the glyph, and repaint from scratch.
                    self.resize(screen)
                    continue
                for event in self.decoder.feed(key):
                    self.handle_key(event)
        finally:
            self.running = False
            for record in self.workstreams.records:
                try:
                    self.workstreams.stop(record['id'])
                except (ValueError, OSError):
                    pass
            self.stop()
            if hasattr(self, 'save_state'): self.save_state(force=True)
            sys.stdout.write(KEYBOARD_DISABLE)
            if self.glyph_graphics:
                sys.stdout.write(kitty_clear()+kitty_clear_previews())
            sys.stdout.flush()
            curses.noraw()


def run_terminal(app):
    # macOS ncurses expands Ghostty's Unicode REP capability to an empty string:
    # repeated rules disappear and subsequent text lands at the wrong column.
    # Ghostty supports xterm-256color; graphics and keyboard detection are separate.
    previous = os.environ.get('TERM')
    if previous == 'xterm-ghostty':
        os.environ['TERM'] = 'xterm-256color'
    try:
        curses.wrapper(app.run)
    finally:
        if previous is not None:
            os.environ['TERM'] = previous
        else:
            os.environ.pop('TERM', None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:18042')
    parser.add_argument('--mute', action='store_true', help='Start without audio playback')
    parser.add_argument('--unmute', action='store_true', help='Enable playback for a previously muted workspace')
    parser.add_argument('--workspace', type=Path, help='Workspace directory for a dedicated window')
    parser.add_argument('--speed', type=float, help='Start at a speech rate from 0.5 to 2')
    args = parser.parse_args()
    if args.workspace:
        os.chdir(args.workspace.expanduser())
    terminal = Terminal(args.url, args.mute)
    if args.unmute and not args.mute:
        terminal.settings.muted = not bool(terminal.client.player_path)
        terminal.mark_dirty()
    if args.speed is not None:
        if not .5 <= args.speed <= 2:
            parser.error('--speed must be between 0.5 and 2')
        terminal.settings.speed = args.speed
        terminal.mark_dirty()
    try:
        run_terminal(terminal)
    except KeyboardInterrupt:
        terminal.client.interrupt()


if __name__ == '__main__':
    main()
