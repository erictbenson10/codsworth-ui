"""Kitty keyboard protocol decoder: physical press/repeat/release, not timers.

Protocol: https://sw.kovidgoyal.net/kitty/keyboard-protocol/
"""
from dataclasses import dataclass
import re
import time

ENABLE = '\x1b[?1003l\x1b[?1002h\x1b[?1006h\x1b[>27u\x1b[?u\x1b[?1004h\x1b[?2004h'
DISABLE = '\x1b[?1003l\x1b[?1002l\x1b[?1006l\x1b[<u\x1b[?1004l\x1b[?2004l'


@dataclass
class Key:
    text: object = ''
    kind: str = 'press'
    modifiers: int = 0
    code: int = 0
    x: int = 0
    y: int = 0


def decode(sequence):
    mouse = re.fullmatch(r'\x1b\[<(\d+);(\d+);(\d+)([Mm])', sequence)
    if mouse:
        button, x, y = map(int, mouse.groups()[:3])
        if mouse[4] == 'm':
            return Key('release',kind='mouse',x=x-1,y=y-1)
        action = {0:'click', 32:'drag', 64:'wheel_up', 65:'wheel_down'}.get(button & ~28)
        return Key(action, kind='mouse', x=x-1, y=y-1) if action else None

    if sequence == '\x1b[O':
        return Key(kind='focus_lost')
    if sequence == '\x1b[I':
        return Key(kind='focus_gained')
    if sequence == '\x1b[Z':
        # Legacy Shift+Tab; terminals without the protocol never send it as a modified '\t'.
        return Key('\t', modifiers=1)
    query = re.fullmatch(r'\x1b\[\?(\d+)u', sequence)
    if query:
        return Key(kind='protocol', code=int(query[1]))
    match = re.fullmatch(r'\x1b\[(\d+)(?::[\d:]*)?(?:;([\d:]*))?(?:;([\d:]+))?u', sequence)
    if match:
        code = int(match[1])
        mods = (match[2] or '1').split(':')
        modifiers = int(mods[0] or '1') - 1
        event = int(mods[1]) if len(mods) > 1 and mods[1] else 1
        if event not in (1, 2, 3):
            return None
        try:
            text = ''.join(chr(int(value)) for value in match[3].split(':')) if match[3] else (chr(code) if code < 57344 else '')
        except (ValueError, OverflowError):
            return None
        text = {57348:'insert',57349:'delete',57350:'left',57351:'right',57352:'up',57353:'down',57354:'page_up',57355:'page_down',57356:'home',57357:'end'}.get(code,text)
        if modifiers & 4 and code in range(97, 123):
            text = chr(code - 96)
        return Key(text, {1:'press', 2:'repeat', 3:'release'}[event], modifiers, code)
    match = re.fullmatch(r'\x1b\[(?:1;(\d+)(?::([123]))?)?([ABCDHF])', sequence)
    if match:
        return Key({'A':'up','B':'down','C':'right','D':'left','H':'home','F':'end'}[match[3]],
                   {None:'press','1':'press','2':'repeat','3':'release'}[match[2]], int(match[1] or '1')-1)
    match = re.fullmatch(r'\x1b\[(1|3|4|7|8)~', sequence)
    if match:
        return Key({'1':'home','3':'delete','4':'end','7':'home','8':'end'}[match[1]])
    # Functional keys retain legacy CSI forms even with the protocol enabled.
    match = re.fullmatch(r'\x1b\[(5|6)(?:;(\d+)(?::([123]))?)?~', sequence)
    if match:
        return Key('page_up' if match[1] == '5' else 'page_down',
                   {None:'press', '1':'press', '2':'repeat', '3':'release'}[match[3]], int(match[2] or '1')-1)
    return None


class Decoder:
    def __init__(self):
        self.pending = ''
        self.started = 0
        self.pasting = False
        self.paste = ''
        self.paste_tail = ''

    def feed(self, value):
        if self.pasting:
            if not isinstance(value, str):
                return []
            self.paste_tail += value
            end = '\x1b[201~'
            if self.paste_tail.endswith(end):
                self.paste += self.paste_tail[:-len(end)]
                result = self.paste[:8000].replace('\r\n', '\n').replace('\r', '\n')
                self.pasting, self.paste, self.paste_tail = False, '', ''
                return [Key(result, kind='paste')]
            if len(self.paste_tail) > len(end):
                self.paste = (self.paste + self.paste_tail[:-len(end)])[:8000]
                self.paste_tail = self.paste_tail[-len(end):]
            return []
        if not isinstance(value, str):
            return [Key(value)]
        if not self.pending:
            if value == '\x1b':
                self.pending, self.started = value, time.monotonic()
                return []
            return [Key(value, code=ord(value) if len(value) == 1 else 0)]
        self.pending += value
        if len(self.pending) == 2 and value != '[':
            self.pending = ''
            return [Key('\x1b', code=27), Key(value)]
        if len(self.pending) > 128:
            self.pending = ''
            return []
        if len(self.pending) > 2 and ('@' <= value <= '~'):
            if self.pending == '\x1b[200~':
                self.pending = ''
                self.pasting = True
                return []
            result = decode(self.pending)
            self.pending = ''
            return [result] if result else []
        return []

    def expire(self):
        # Only disambiguate a standalone Escape key; never infer Space release.
        if self.pending and time.monotonic()-self.started > .05:
            escape = self.pending == '\x1b'
            self.pending = ''
            return [Key('\x1b', code=27)] if escape else []
        return []
