"""Visual export regressions: clipping, transparency and format consistency."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'glyphs' / 'codsworth.py'
spec = importlib.util.spec_from_file_location('glyph', SCRIPT)
glyph = importlib.util.module_from_spec(spec)
spec.loader.exec_module(glyph)


class GlyphTests(unittest.TestCase):
    def test_camera_aperture_contracts_from_all_sides_with_fixed_barrel(self):
        opened, stopped = glyph.Grid(), glyph.Grid()
        opened.optic(20, 20, 9, aperture=.95)
        stopped.optic(20, 20, 9, aperture=.1)
        open_hole = {(y, x) for y in range(14, 27) for x in range(14, 27)
                     if opened.g[y][x] == glyph.GLASS}
        small_hole = {(y, x) for y in range(14, 27) for x in range(14, 27)
                      if stopped.g[y][x] == glyph.GLASS}
        self.assertIn((20, 20), small_hole)
        self.assertGreater(len(open_hole), len(small_hole) * 2)
        for dy, dx in ((-3, 0), (3, 0), (0, -3), (0, 3)):
            self.assertEqual(opened.g[20 + dy][20 + dx], glyph.GLASS)
            self.assertNotEqual(stopped.g[20 + dy][20 + dx], glyph.GLASS)
        for y in range(9, 32):
            for x in range(9, 32):
                if (y - 20) ** 2 + (x - 20) ** 2 > 36:
                    self.assertEqual(opened.g[y][x], stopped.g[y][x])

    def test_fractional_optic_positions_do_not_create_striped_irises(self):
        fractional, snapped = glyph.Grid(), glyph.Grid()
        fractional.optic(20.5, 20.5, 9, aperture=.6, shutter=.4)
        snapped.optic(20, 20, 9, aperture=.6, shutter=.4)
        self.assertEqual(fractional.g, snapped.g)

    def test_annoyed_shutter_preserves_round_exterior_and_closes_upper_lens(self):
        plain, annoyed = glyph.Grid(), glyph.Grid()
        plain.optic(20, 20, 9, aperture=.75)
        annoyed.optic(20, 20, 9, aperture=.75, shutter=1)
        self.assertEqual(plain.g[18][20], glyph.GLASS)
        self.assertNotEqual(annoyed.g[18][20], glyph.GLASS)
        self.assertEqual(plain.g[22][20], annoyed.g[22][20])
        for y in range(8, 33):
            for x in range(8, 33):
                if (y-20)**2 + (x-20)**2 >= 36:
                    self.assertEqual(plain.g[y][x], annoyed.g[y][x])

    def test_expressions_and_thinking_change_body_below_the_optics(self):
        bodies = [repr(glyph.frames(expression=mood)[0][58:])
                  for mood in glyph.EXPRESSIONS]
        self.assertEqual(len(set(bodies)), len(bodies))
        self.assertNotEqual(glyph.frames(state='thinking')[0][58:],
                            glyph.frames(state='idle')[0][58:])

    def test_actions_have_contact_and_completion_and_do_not_clip(self):
        for name in ('open-panel', 'present-result', 'inspect-error'):
            animation, cues = glyph.action_frames(name)
            self.assertEqual(len(animation), len(cues))
            self.assertEqual(cues[0]['progress'], 0)
            self.assertEqual(cues[-1]['progress'], 1)
            self.assertTrue(any(cue['contact'] for cue in cues))
            self.assertFalse(cues[0]['contact'])
            for grid in animation:
                edge = grid[0] + grid[-1] + [row[0] for row in grid] + [row[-1] for row in grid]
                self.assertTrue(all(pixel == glyph.BG for pixel in edge), name)

    def test_rear_tool_does_not_cross_the_gripper_clearance(self):
        grid = glyph.frame()
        clearance = [grid[y][x] for y in range(glyph.CY + 19, glyph.CY + 27)
                     for x in range(73, 94)]
        self.assertTrue(all(pixel == glyph.BG for pixel in clearance))

    def test_named_states_are_distinct_and_never_clipped(self):
        animations = []
        for expression in ('neutral', 'pleased', 'skeptical', 'concerned', 'annoyed'):
            for state in ('idle', 'talking', 'listening', 'thinking'):
                animation = glyph.frames(state=state, expression=expression)
                animations.append(repr(animation))
                for index, grid in enumerate(animation):
                    edge = grid[0] + grid[-1] + [row[0] for row in grid] + [row[-1] for row in grid]
                    self.assertTrue(all(pixel == glyph.BG for pixel in edge), (expression, state, index))
                    # Browser entry hop (-2) and lower hover (+4) retain a clear border.
                    occupied = [y for y, row in enumerate(grid) if any(pixel != glyph.BG for pixel in row)]
                    self.assertGreater(min(occupied) - 2, 0)
                    self.assertLess(max(occupied) + 4, glyph.H - 1)
        self.assertEqual(len(set(animations)), len(animations))

    def test_invalid_scale_is_rejected_before_writing_assets(self):
        for value in ('0', '-2'):
            with self.subTest(scale=value), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), '--out', directory, '--px', value],
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn('positive integer', result.stderr)
                self.assertFalse(list(Path(directory).iterdir()))

    def test_entire_animation_stays_inside_canvas(self):
        for index, grid in enumerate(glyph.frames()):
            edge = grid[0] + grid[-1] + [row[0] for row in grid] + [row[-1] for row in grid]
            self.assertTrue(all(pixel == glyph.BG for pixel in edge),
                            f'Frame {index} clips the canvas edge')

    def test_exports_share_scale_colors_and_transparency(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run([sys.executable, str(SCRIPT), '--out', directory, '--px', '2'],
                           check=True, capture_output=True)
            out = Path(directory)
            with Image.open(out / 'codsworth.png') as png:
                self.assertEqual(png.mode, 'RGBA')
                self.assertEqual(png.getpixel((0, 0))[3], 0)
                actual = png.copy()
            svg = ET.parse(out / 'codsworth.svg').getroot()
            self.assertEqual((int(svg.attrib['width']), int(svg.attrib['height'])), actual.size)
            opaque_pixels = sum(alpha == 255 for alpha in actual.getchannel('A').tobytes())
            self.assertEqual(sum(int(rect.attrib['width']) * int(rect.attrib['height'])
                                 for rect in svg), opaque_pixels)
            for rect in svg:
                x, y = int(rect.attrib['x']), int(rect.attrib['y'])
                value = rect.attrib['fill'].lstrip('#')
                rgb = tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
                self.assertEqual(actual.getpixel((x, y)), rgb + (255,))
            with Image.open(out / 'codsworth.gif') as gif:
                self.assertEqual(gif.size, actual.size)
                self.assertGreater(gif.n_frames, 1)
                self.assertEqual(gif.info['loop'], 0)
                self.assertEqual(gif.convert('RGBA').tobytes(), actual.tobytes())
                for index in range(gif.n_frames):
                    gif.seek(index)
                    alpha = gif.convert('RGBA').getchannel('A')
                    left, top, right, bottom = alpha.getbbox()
                    self.assertGreater(left, 0)
                    self.assertGreater(top, 0)
                    self.assertLess(right, gif.width)
                    self.assertLess(bottom, gif.height)


if __name__ == '__main__':
    unittest.main()
