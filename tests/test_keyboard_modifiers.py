"""What the real decoder produces for the tray-binding candidate keys.

Every sequence here is fed through the genuine `keyboard.Decoder` one character
at a time, because that is exactly how `agent/terminal.py` drives it: the curses
loop calls `screen.get_wch()` and passes each single character to
`decoder.feed(key)` (terminal.py `run()`).  Nothing about the decoder is mocked.

Where the byte sequences come from
---------------------------------
`keyboard.ENABLE` is `\x1b[>27u...`; 27 = 1|2|8|16 = disambiguate + report event
types + report all keys as escape codes + report associated text.  With flag 8
set, a conforming terminal (Ghostty, kitty) reports *every* key as a CSI-u
sequence `CSI <unicode-key-code> [:<alternates>] [;<modifiers>[:<event-type>]]
[;<text-codepoints>] u` -- which is precisely the shape of the regex on
keyboard.py line 32.  So the press form is `\x1b[<codepoint>;<mods+1>u`, and the
codepoint is the *unshifted ASCII* of the physical key: '\\'=92, '1'..'6'=49..54,
'a'=97, tab=9, space=32.

The modifier field is a 1-based bitmask (keyboard.py subtracts 1):
1=Shift, 2=Alt, 4=Ctrl, 8=Meta -- so ";3u" == Alt, ";5u" == Ctrl, ";2u" ==
Shift.  Event type is the ":N" suffix on the modifier field: 1=press, 2=repeat,
3=release, per keyboard.py line 37/47.

With the protocol OFF a terminal sends no CSI-u at all.  Ghostty on macOS with
`macos-option-as-alt = true` (the setting in use on this machine) emits Alt+X as
the two bytes ESC then X -- the classic "meta prefix" -- which is the branch on
keyboard.py line 94.  Ctrl+letter is a bare C0 control byte.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'agent'))

from keyboard import Decoder, Key, decode  # noqa: E402


def press(sequence):
    """Feed a sequence character-by-character, exactly as the curses loop does."""
    decoder = Decoder()
    events = []
    for character in sequence:
        events.extend(decoder.feed(character))
    return events


def only(sequence):
    events = press(sequence)
    assert len(events) == 1, f'expected one key from {sequence!r}, got {events!r}'
    return events[0]


class KittyProtocolAltKeys(unittest.TestCase):
    """Alt+\\ and Alt+1..6 with the Kitty protocol enabled."""

    def test_alt_backslash_keeps_the_alt_bit_and_is_distinguishable_from_a_typed_backslash(self):
        # Ghostty/kitty: physical '\' is codepoint 92, Alt => modifier field 3.
        self.assertEqual(only('\x1b[92;3u'), Key(text='\\', kind='press', modifiers=2, code=92))
        # A plain, unmodified backslash differs only in `modifiers`, so any
        # binding MUST test modifiers, not text.
        self.assertEqual(only('\x1b[92u'), Key(text='\\', kind='press', modifiers=0, code=92))

    def test_alt_digits_one_through_six_keep_the_alt_bit(self):
        for index, code in enumerate(range(49, 55), start=1):
            with self.subTest(digit=index):
                self.assertEqual(only(f'\x1b[{code};3u'),
                                 Key(text=str(index), kind='press', modifiers=2, code=code))

    def test_the_ctrl_letter_rewrite_does_not_touch_backslash_or_digits(self):
        # keyboard.py line 45 rewrites text only when Ctrl is held AND the code
        # is a lowercase letter.  Alt-only never enters that branch, and 92 /
        # 49-54 are outside range(97, 123) regardless.
        self.assertEqual(only('\x1b[92;3u').text, '\\')
        self.assertEqual(only('\x1b[49;3u').text, '1')
        # Even Ctrl+Alt+backslash is left alone (modifier field 7 = Alt|Ctrl).
        self.assertEqual(only('\x1b[92;7u'), Key(text='\\', kind='press', modifiers=6, code=92))

    def test_alt_letter_is_not_rewritten_into_ctrl_letter(self):
        # The concern that Alt+letter becomes Ctrl+letter is unfounded: the
        # guard is `modifiers & 4` (Ctrl), not `& 2` (Alt).
        self.assertEqual(only('\x1b[97;3u'), Key(text='a', kind='press', modifiers=2, code=97))

    def test_alt_keys_also_report_repeat_and_release(self):
        self.assertEqual(only('\x1b[92;3:3u'), Key(text='\\', kind='release', modifiers=2, code=92))
        self.assertEqual(only('\x1b[49;3:2u'), Key(text='1', kind='repeat', modifiers=2, code=49))


class PlainTerminalAltKeys(unittest.TestCase):
    """The same keys with the protocol OFF: Ghostty sends ESC + the character."""

    def test_alt_backslash_decomposes_into_escape_plus_backslash(self):
        # This is the meta-prefix branch, keyboard.py line 94-96.  The Alt bit
        # is GONE and an extra Escape key is delivered first -- and Escape is
        # bound to `self.stop()` in terminal.handle_key.
        self.assertEqual(press('\x1b\\'), [Key(text='\x1b', kind='press', modifiers=0, code=27),
                                           Key(text='\\', kind='press', modifiers=0, code=0)])

    def test_alt_digits_decompose_into_escape_plus_the_digit(self):
        for digit in '123456':
            with self.subTest(digit=digit):
                self.assertEqual(press('\x1b' + digit),
                                 [Key(text='\x1b', kind='press', modifiers=0, code=27),
                                  Key(text=digit, kind='press', modifiers=0, code=0)])

    def test_a_bare_escape_is_indistinguishable_until_the_expire_timeout(self):
        # Nothing is emitted for a lone ESC; Decoder.expire() resolves it after
        # 50ms.  So ESC-prefixed Alt cannot be told apart from a user pressing
        # Escape and then typing, except by timing.
        decoder = Decoder()
        self.assertEqual(decoder.feed('\x1b'), [])


class AlwaysAvailableFallback(unittest.TestCase):
    def test_ctrl_t_arrives_as_0x14_with_and_without_the_protocol(self):
        # Protocol on: 't' is 116, Ctrl => modifier field 5; keyboard.py line 45
        # folds it to the C0 control character.
        self.assertEqual(only('\x1b[116;5u'), Key(text='\x14', kind='press', modifiers=4, code=116))
        # Protocol off: the terminal sends the raw control byte itself.
        self.assertEqual(press('\x14'), [Key(text='\x14', kind='press', modifiers=0, code=20)])

    def test_ctrl_b_and_ctrl_g_behave_identically(self):
        self.assertEqual(only('\x1b[98;5u'), Key(text='\x02', kind='press', modifiers=4, code=98))
        self.assertEqual(only('\x1b[103;5u'), Key(text='\x07', kind='press', modifiers=4, code=103))
        self.assertEqual(press('\x02'), [Key(text='\x02', kind='press', modifiers=0, code=2)])
        self.assertEqual(press('\x07'), [Key(text='\x07', kind='press', modifiers=0, code=7)])


class ExistingBindingsMustNotRegress(unittest.TestCase):
    """Keys terminal.handle_key already dispatches on."""

    def test_shift_tab_legacy_and_protocol_forms_both_give_tab_with_shift(self):
        # Legacy CSI Z, special-cased on keyboard.py line 26.
        self.assertEqual(only('\x1b[Z'), Key(text='\t', kind='press', modifiers=1, code=0))
        # Protocol form: tab is codepoint 9, Shift => modifier field 2.
        self.assertEqual(only('\x1b[9;2u'), Key(text='\t', kind='press', modifiers=1, code=9))

    def test_ctrl_tab_keeps_tab_text_and_the_ctrl_bit(self):
        # 9 is outside range(97,123) so the Ctrl rewrite leaves the text alone.
        self.assertEqual(only('\x1b[9;5u'), Key(text='\t', kind='press', modifiers=4, code=9))

    def test_ctrl_letter_editing_keys_fold_to_their_control_characters(self):
        expected = {97: '\x01', 101: '\x05', 117: '\x15', 107: '\x0b', 119: '\x17'}
        for code, character in expected.items():
            with self.subTest(letter=chr(code)):
                self.assertEqual(only(f'\x1b[{code};5u'),
                                 Key(text=character, kind='press', modifiers=4, code=code))
                # And without the protocol the bare control byte is equivalent
                # for terminal.handle_key, which dispatches on `text`.
                self.assertEqual(press(character)[0].text, character)

    def test_plain_space_press_and_release_drive_push_to_talk(self):
        # terminal.handle_key gates push-to-talk on `code == 32 and
        # modifiers & 0x3f == 0`, so both fields matter.
        self.assertEqual(only('\x1b[32u'), Key(text=' ', kind='press', modifiers=0, code=32))
        # Release form: event type 3 on an otherwise unmodified key.
        self.assertEqual(only('\x1b[32;1:3u'), Key(text=' ', kind='release', modifiers=0, code=32))
        # A space with Alt held is NOT push-to-talk, and stays distinguishable.
        self.assertEqual(only('\x1b[32;3u'), Key(text=' ', kind='press', modifiers=2, code=32))

    def test_protocol_response_still_reports_the_flags_terminal_py_checks(self):
        # terminal.handle_key sets key_protocol = event.code & 10 == 10.
        self.assertEqual(decode('\x1b[?27u'), Key(text='', kind='protocol', modifiers=0, code=27))
        self.assertTrue(decode('\x1b[?27u').code & 10 == 10)
        self.assertFalse(decode('\x1b[?1u').code & 10 == 10)


if __name__ == '__main__':
    unittest.main()
