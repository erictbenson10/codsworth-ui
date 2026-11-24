import base64
import io
import os
from pathlib import Path
import queue
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))
import image_attachments as attachments
import terminal
from keyboard import Decoder, Key

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aOioAAAAASUVORK5CYII=')


class ImageFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'Screen shot café.png'
        self.path.write_bytes(PNG)


class ImageAttachmentTests(ImageFixture, unittest.TestCase):
    def test_finder_paths_shell_escaping_quotes_and_file_urls(self):
        for dropped in (str(self.path), shlex.quote(str(self.path)),
                        str(self.path).replace(' ', '\\ '), self.path.as_uri()):
            with self.subTest(dropped=dropped):
                draft = attachments.normalize_drop(dropped)
                text, images = attachments.prepare_message('Explain this ' + draft, self.root)
                self.assertEqual(text, 'Explain this')
                self.assertEqual(images[0]['name'], self.path.name)
                self.assertEqual(images[0]['mime_type'], 'image/png')
                self.assertEqual(base64.b64decode(images[0]['data']), PNG)

    def test_multiple_images_and_relative_paths_preserve_message(self):
        second = self.root / 'two.PNG'
        second.write_bytes(PNG)
        text, images = attachments.prepare_message(
            f"What's different? {shlex.quote(str(self.path))}\n./two.PNG", self.root)
        self.assertEqual(text, "What's different?")
        self.assertEqual([image['name'] for image in images], [self.path.name, second.name])
        self.assertEqual(attachments.prepare_message('./two.PNG', self.root)[0], 'Describe the attached images.')
        self.assertFalse(attachments.is_command(str(second)))
        self.assertTrue(attachments.is_command('/export ' + str(second)))

    def test_ordinary_paste_remote_urls_and_prose_do_not_read_files(self):
        for text in ("What's new?", 'https://example.com/image.png', 'file://remote/a.png',
                     'image.png is missing', '/help', 'first\n/speed 2\n'):
            with patch.object(attachments, 'read_image') as read:
                self.assertEqual(attachments.prepare_message(text, self.root), (text.strip(), []))
                read.assert_not_called()

    def test_overlong_dropped_path_remains_an_editable_draft(self):
        path = '/' + 'x' * 4000 + '.png'
        self.assertEqual(attachments.normalize_drop(path), path)
        with self.assertRaisesRegex(ValueError, 'Cannot read image'):
            attachments.prepare_message(path, self.root)

    def test_missing_invalid_oversize_special_files_and_count_limits(self):
        with self.assertRaisesRegex(ValueError, 'Cannot read'):
            attachments.prepare_message('./missing.png', self.root)
        self.path.write_bytes(b'not an image')
        with self.assertRaisesRegex(ValueError, 'PNG, JPEG'):
            attachments.prepare_message(shlex.quote(str(self.path)), self.root)
        self.path.write_bytes(PNG)
        with patch.object(attachments, 'MAX_IMAGE_BYTES', 10):
            with self.assertRaisesRegex(ValueError, '4 MiB'):
                attachments.prepare_message(shlex.quote(str(self.path)), self.root)
        with patch.object(attachments, 'MAX_TOTAL_BYTES', len(PNG)):
            with self.assertRaisesRegex(ValueError, '6 MiB'):
                attachments.prepare_message((shlex.quote(str(self.path))+' ') * 2, self.root)
        with self.assertRaisesRegex(ValueError, 'up to 4'):
            attachments.prepare_message((shlex.quote(str(self.path))+' ') * 5, self.root)
        special = self.root / 'pipe.png'
        os.mkfifo(special)
        with self.assertRaisesRegex(ValueError, 'regular image'):
            attachments.prepare_message(str(special), self.root)

    def test_shell_metacharacters_are_literal_filename_data(self):
        path = self.root / '$(touch SHOULD_NOT_EXIST) `whoami`.png'
        path.write_bytes(PNG)
        with patch.object(terminal.subprocess, 'run') as run:
            _, images = attachments.prepare_message(shlex.quote(str(path)), self.root)
            run.assert_not_called()
        self.assertEqual(images[0]['name'], path.name)

    def test_image_content_type_is_sniffed_and_encoded_payload_checked(self):
        self.assertEqual(attachments.image_mime(PNG), 'image/png')
        for data, mime in ((b'\xff\xd8\xffrest', 'image/jpeg'), (b'GIF89arest', 'image/gif'),
                           (b'RIFF1234WEBPrest', 'image/webp')):
            self.assertEqual(attachments.image_mime(data), mime)
        with self.assertRaisesRegex(ValueError, 'encoding'):
            attachments.decode_image('%%%', 'image/png')
        with self.assertRaisesRegex(ValueError, 'media type'):
            attachments.decode_image(base64.b64encode(PNG).decode(), 'image/jpeg')


class TerminalImageTests(ImageFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = terminal.Terminal('http://localhost:1', True, self.root / 'state')

    def drop(self, path):
        decoder = Decoder()
        for character in '\x1b[200~' + path + '\x1b[201~':
            for event in decoder.feed(character):
                self.app.handle_key(event)

    def test_drop_waits_for_enter_and_sends_image_bytes_with_message(self):
        self.app.input = 'Explain this'
        with patch.object(self.app.client, 'submit', return_value=1) as submit:
            self.drop(str(self.path))
            submit.assert_not_called()
            self.assertIn('Image paths added', self.app.status)
            self.app.handle_key(Key('\r'))
        self.assertEqual(submit.call_args.args, ('Explain this',))
        self.assertEqual(base64.b64decode(submit.call_args.kwargs['images'][0]['data']), PNG)
        self.assertEqual(self.app.input, '')
        self.assertNotIn('base64', self.app.state.path.read_text())
        self.assertIn(str(self.path), self.app.transcript[-2][1])

    def test_image_only_absolute_path_is_a_message_and_can_be_removed(self):
        path = self.root / 'screenshot.png'
        path.write_bytes(PNG)
        self.drop(str(path))
        with patch.object(self.app.client, 'submit', return_value=1) as submit:
            self.app.send()
            self.assertEqual(submit.call_args.args, ('Describe the attached images.',))
            self.drop(str(path))
            self.app.edit_input('No image now', 12)
            self.app.send()
            self.assertEqual(submit.call_args.kwargs, {})

    def test_draft_survives_invalid_file_and_reopen_without_reading_images(self):
        self.drop(str(self.path))
        original = self.app.input
        self.app.save_state(force=True)
        with patch.object(attachments, 'read_image') as read:
            restored = terminal.Terminal('http://localhost:1', True, self.root / 'state')
            self.assertEqual(restored.input, original)
            read.assert_not_called()
        self.path.unlink()
        with patch.object(restored.client, 'submit') as submit:
            restored.send()
            submit.assert_not_called()
        self.assertEqual(restored.input, original)
        self.assertIn('Cannot read image', restored.status)

    def test_drop_in_middle_of_text_and_full_draft_keep_paths_whole(self):
        self.app.input = 'Compare these'
        self.app.cursor = 7
        self.drop(str(self.path))
        text, images = attachments.prepare_message(self.app.input, self.root)
        self.assertEqual(text, 'Compare  these')
        self.assertEqual(len(images), 1)
        self.app.input = 'x' * 7990
        self.drop(str(self.path))
        self.assertEqual(self.app.input, 'x' * 7990)
        self.assertIn('do not fit', self.app.status)

    def test_focused_stream_images_and_drafts_are_isolated_from_home(self):
        self.drop(str(self.path))
        home_draft = self.app.input
        record = self.app.workstreams.create('Image review')
        self.app.workstreams.focus(None)
        self.app.focus_stream(record['id'])
        self.assertEqual(self.app.input, '')
        self.drop(str(self.path))
        client = self.app.workstreams._client(record)
        with patch.object(client, 'submit', return_value=1) as submit, patch.object(self.app.client, 'submit') as home:
            self.app.send()
            home.assert_not_called()
            self.assertEqual(base64.b64decode(submit.call_args.kwargs['images'][0]['data']), PNG)
        self.app.focus_stream(None)
        self.assertEqual(self.app.input, home_draft)
        self.assertNotIn('base64', self.app.workstreams.path.read_text())

    def test_composer_shows_image_count_without_reading_file(self):
        self.drop(str(self.path))
        drawn = []
        class Screen:
            def getmaxyx(self): return 40, 100
            def erase(self): pass
            def refresh(self): pass
            def addnstr(self, y, x, text, n, attr): drawn.append(text[:n])
            def move(self, *args): pass
        with patch.object(terminal.curses, 'color_pair', return_value=0), patch.object(terminal.sys, 'stdout', new=io.StringIO()), patch.object(attachments, 'read_image') as read:
            self.app.draw(Screen())
            read.assert_not_called()
        self.assertTrue(any('1 image · Enter send · delete path to detach' in text for text in drawn))
        self.assertTrue(any(self.path.name in text for text in drawn))

    def test_preview_card_reads_png_header(self):
        card = attachments.preview_card(self.path)
        self.assertEqual(card['name'], self.path.name)
        self.assertEqual(card['mime'], 'image/png')
        self.assertEqual((card['width'], card['height']), (1, 1))
        self.assertIsNone(card['error'])
        self.assertIn('PNG', attachments.preview_label(card))
        missing = attachments.preview_card(self.root / 'gone.png')
        self.assertEqual(missing['error'], 'missing')
        self.assertEqual(attachments.image_size(PNG), (1, 1))
        gif = b'GIF89a' + (12).to_bytes(2, 'little') + (34).to_bytes(2, 'little')
        self.assertEqual(attachments.image_size(gif), (12, 34))

    def test_client_posts_images_only_on_the_attached_turn(self):
        client = terminal.Client('http://localhost:1', terminal.Settings(muted=True), queue.Queue())
        _, images = attachments.prepare_message(shlex.quote(str(self.path)), self.root)
        captured = []
        def events(payload, turn):
            captured.append(payload)
            yield dict(type='done')
        with patch.object(client, 'chat_events', side_effect=events), patch.object(client, 'speak'):
            client.run(terminal.Turn(1), 'Review', images)
            client.run(terminal.Turn(2), 'Follow up')
        self.assertEqual(captured[0]['images'], images)
        self.assertNotIn('images', captured[1])


if __name__ == '__main__':
    unittest.main()
