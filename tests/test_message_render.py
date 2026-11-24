import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agent'))
from message_render import cell_width,render_message,wrap_cells,highlight_metrics


class MessageRenderTests(unittest.TestCase):
    def test_summary_highlights_counts_and_ids_without_altering_text_or_markup(self):
        text='24h: 35 queued / 23 PR · 2 open: qf1f9bf (#1838) · 2.4M/min · model3'
        spans=highlight_metrics([(text,'body'),(' 64 withheld','strong'),(' 127','inline_code')])
        self.assertEqual(''.join(part for part,role in spans),text+' 64 withheld 127')
        self.assertEqual([part for part,role in spans if role=='metric'],['24h','35','23','2','2.4M'])
        self.assertIn(('qf1f9bf','inline_code'),spans)
        self.assertIn(('#1838','inline_code'),spans)
        self.assertIn((' 64 withheld','strong'),spans)
        self.assertIn((' 127','inline_code'),spans)

    def test_code_preserves_indentation_and_literal_markup(self):
        lines=render_message('```python\n    print("**literal**")\n```',60)
        self.assertIn(('    print("**literal**")','code'),lines)
        self.assertNotIn('```','\n'.join(text for text,_ in lines))
        self.assertEqual(lines[0],('python','muted'))

    def test_heading_list_inline_code_and_link_remain_readable(self):
        lines=render_message('# Result\n- Use `config.py` and **save**.\n[Docs](https://example.com/docs)',60)
        self.assertIn(('Result','heading'),lines)
        self.assertIn(('- Use config.py and save.','body'),lines)
        self.assertIn(('Docs (https://example.com/docs)','body'),lines)

    def test_unicode_wrap_is_bounded_and_lossless(self):
        original='界界界e\u0301XYZ'
        lines=wrap_cells(original,5)
        self.assertEqual(''.join(lines),original)
        self.assertTrue(all(sum(cell_width(c) for c in line)<=5 for line in lines))

    def test_list_continuation_is_indented_and_compact_bounded(self):
        lines=render_message('- A long list item with several words',12)
        self.assertTrue(all(len(text)<=12 for text,_ in lines))
        self.assertTrue(lines[1][0].startswith('  '))

    def test_unclosed_fence_keeps_rest_literal_without_crash(self):
        lines=render_message('~~~sh\n  echo `literal`\n# not a heading',60)
        self.assertEqual(lines[-1],('# not a heading','code'))
        self.assertIn(('  echo `literal`','code'),lines)

    def test_inline_styles_survive_wrapping_and_wide_characters(self):
        rows=render_message('Use **界界 carefully here** and `config.py`.',12,styled=True)
        text=''.join(line for line,_,_ in rows)
        self.assertEqual(text,'Use 界界 carefully here and config.py.')
        strong=''.join(part for _,_,parts in rows for part,role in parts if role=='strong')
        self.assertEqual(strong,'界界 carefully here')
        self.assertEqual(''.join(part for _,_,parts in rows for part,role in parts if role=='inline_code'),'config.py')
        for line,_,parts in rows:
            self.assertEqual(''.join(part for part,_ in parts),line)
            self.assertLessEqual(sum(cell_width(c) for c in line),12)
