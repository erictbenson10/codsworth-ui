import io
import json
import threading
import unittest
from unittest.mock import patch
from test_terminal import terminal
from diagnostics import collect, format_checks


class DiagnosticsTests(unittest.TestCase):
    def test_read_only_probes_and_honest_device_limit(self):
        calls=[]
        def opener(request,timeout):
            calls.append(request)
            data={'type':'fleet_snapshot','available':{'health':True},'errors':[]} if request.full_url.endswith('/overview') else {'ready':True}
            return io.BytesIO(json.dumps(data).encode())
        with patch('diagnostics.shutil.which',return_value='/binary'):
            result=collect('http://localhost:1234',opener=opener)
        self.assertEqual(len(calls),3)
        self.assertTrue(all(request.get_method()=='GET' and request.data is None for request in calls))
        text=format_checks(result)
        self.assertIn('Microphone permission / output device: not tested',text)
        self.assertIn('Fleet: ok',text)

    def test_failures_not_ready_and_cancel_are_distinct(self):
        with patch('diagnostics.shutil.which',return_value=None):
            result=collect('http://localhost:1234',opener=lambda *a,**k:io.BytesIO(b'{"ready":false}'))
        self.assertIn('Audio player: missing',format_checks(result))
        self.assertIn('Voice service: attention',format_checks(result))
        cancel=threading.Event();cancel.set()
        with patch('diagnostics.urllib.request.urlopen') as network:
            collect('http://localhost:1234',cancel)
            network.assert_not_called()
