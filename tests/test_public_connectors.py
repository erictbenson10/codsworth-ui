"""Public helper modules do not assume credentials from a private deployment."""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch


class PublicConnectorTests(unittest.TestCase):
    def test_gitea_requires_an_explicit_credential_path_before_file_or_network_access(self):
        source = Path(__file__).resolve().parents[1] / 'agent/plugins/cods-gitea/connector.py'
        spec = importlib.util.spec_from_file_location('public_gitea_connector', source)
        connector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(connector)
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(connector.Path, 'stat') as stat, \
             patch.object(connector.urllib.request, 'build_opener') as opener:
            with self.assertRaisesRegex(ValueError, 'credential file is not configured'):
                connector.request('GET', '/repos/example/workspace/issues', None)
        stat.assert_not_called()
        opener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
