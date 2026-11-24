"""Run the public launcher with an isolated Python/SSH command harness."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        python = self.bin / 'python3'
        python.write_text(f'#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
        python.chmod(0o755)
        ssh = self.bin / 'ssh'
        ssh.write_text('#!/bin/sh\necho "Unexpected SSH invocation" >&2\nexit 99\n')
        ssh.chmod(0o755)
        self.env = {**os.environ, 'PATH':str(self.bin)+':/usr/bin:/bin'}
        self.env.pop('CODS_URL',None)

    def launch(self, *args, launcher=None):
        result = subprocess.run([str(launcher or ROOT/'scripts/cods'),*args],
                                cwd=self.root,env=self.env,capture_output=True,text=True,check=True)
        self.assertEqual(result.stderr,'')
        return json.loads(result.stdout)

    def test_default_gateway_is_loopback_without_network_setup(self):
        argv=self.launch('--mute')
        self.assertEqual(argv,[str(ROOT/'agent/terminal.py'),'--url','http://127.0.0.1:18042','--mute'])

    def test_environment_gateway_is_preserved_as_one_argument(self):
        address='https://gateway.example/api?first=one&second=two'
        self.env['CODS_URL']=address
        argv=self.launch('--mute')
        self.assertEqual(argv[1:],[ '--url',address,'--mute'])

    def test_explicit_url_follows_environment_default_for_argparse_override(self):
        self.env['CODS_URL']='https://default.example'
        argv=self.launch('--url','https://chosen.example','--mute')
        import argparse
        parser=argparse.ArgumentParser()
        parser.add_argument('--url')
        parser.add_argument('--mute',action='store_true')
        parsed=parser.parse_args(argv[1:])
        self.assertEqual(parsed.url,'https://chosen.example')
        self.assertTrue(parsed.mute)

    def test_checkout_path_with_spaces_is_resolved_from_launcher_location(self):
        checkout=self.root/'project with spaces'
        (checkout/'scripts').mkdir(parents=True)
        launcher=checkout/'scripts/cods'
        shutil.copy2(ROOT/'scripts/cods',launcher)
        argv=self.launch('--mute',launcher=launcher)
        self.assertEqual(argv[0],str(checkout/'agent/terminal.py'))


if __name__ == '__main__':
    unittest.main()
