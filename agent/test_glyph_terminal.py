import base64
import hashlib
import re
import unittest
from agent.glyph_terminal import frame_rgba,frame_png,fit_cells,kitty_at,kitty_frame,kitty_clear,kitty_clear_previews,kitty_file,render_cells

class Tests(unittest.TestCase):
    def test_actual_frames_match_independent_pillow_crop_goldens(self):
        # These RGBA hashes were independently computed from the source sprite
        # sheets with Pillow, not from this helper's PNG decoder.
        expected={
            'neutral':'79a8f1abc658c1495b8c053ecb602398645af7b128d60e2db99ebdca80bd8edf',
            'pleased':'f4f506c6d6a638028f233dac873bbd6439adb96bc0b74a9c16035b4033f56bf5',
            'skeptical':'6b15143aa513eb57bac241a2a73d6fe85a580e756c58d0269548d28d1d681556',
            'concerned':'8a4043cfb83e85d7794ba89809896c5911469ac0007c21fdd0007ac7f4e4c29e',
            'annoyed':'f96e4376b996566c9ff79164e6c49386c8974e8ee289168e1720f2e2ec71f2fd',
        }
        for expression,digest in expected.items():
            width,height,rows=frame_rgba(expression,'idle',0)
            self.assertEqual((width,height),(96,112))
            data=b''.join(rows)
            self.assertEqual(hashlib.sha256(data).hexdigest(),digest)
            self.assertIn(0,data[3::4])
            self.assertIn(255,data[3::4])
        self.assertNotEqual(frame_png('neutral','talking',0),frame_png('neutral','talking',8))

    def test_protocol_payload_and_fallback(self):
        command=kitty_frame('annoyed','thinking',2,40,24)
        chunks=re.findall(r'\x1b_G([^;]+);([^\x1b]*)\x1b\\',command)
        self.assertTrue(chunks)
        self.assertIn('q=2',chunks[0][0])
        self.assertIn('C=1',chunks[0][0])
        self.assertIn('m=0',chunks[-1][0])
        self.assertEqual(base64.b64decode(''.join(data for _,data in chunks)),frame_png('annoyed','thinking',2))
        self.assertIn('i=31',kitty_clear())
        self.assertIn('i=32',kitty_clear_previews())
        placed=kitty_file('/tmp/shot.png',8,4,32)
        self.assertIn('t=f',placed)
        self.assertIn('i=32',placed)
        self.assertTrue(kitty_at(10,4,placed).startswith('\x1b7\x1b[11;5H'))
        cells=render_cells('neutral','idle',0,40,24)
        columns,rows=fit_cells(40,24)
        self.assertEqual((len(cells),len(cells[0])),(rows,columns))
        self.assertTrue(all(c=='▀' and 0<=fg<256 and 0<=bg<256 for row in cells for c,fg,bg in row))
        self.assertGreater(len({fg for row in cells for _,fg,_ in row}),5)
        self.assertEqual(frame_png('bogus','bogus',0),frame_png('neutral','idle',0))

    def test_small_terminal_fit_preserves_sprite_aspect(self):
        for width,height in ((26,15),(48,28),(15,7),(80,40)):
            columns,rows=fit_cells(width,height)
            self.assertLessEqual(columns,width)
            self.assertLessEqual(rows,height)
            self.assertAlmostEqual(columns/(rows*2),96/112,delta=.08)

if __name__=='__main__':unittest.main()
