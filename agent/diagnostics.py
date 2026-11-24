"""Read-only local/service checks. Never records audio, reads tokens or submits work."""
import json
import platform
import shutil
import urllib.error
import urllib.request


def collect(url, cancelled=None, opener=None):
    opener = opener or urllib.request.urlopen
    checks = [{'name': 'Python', 'status': 'info', 'detail': platform.python_version()},
              {'name': 'Audio player', 'status': 'ok' if shutil.which('ffplay') else 'missing', 'detail': 'ffplay'},
              {'name': 'Microphone encoder', 'status': 'ok' if shutil.which('ffmpeg') else 'missing', 'detail': 'ffmpeg'},
              {'name': 'Microphone permission / output device', 'status': 'not tested',
               'detail': 'Hold Space for a recording test; these checks do not open audio devices.'}]
    for name, path in [('Voice service', '/api/health'), ('Hermes gateway', '/api/chat/health'), ('Fleet', '/api/fleet/overview')]:
        if cancelled and cancelled.is_set():
            break
        try:
            request = urllib.request.Request(url.rstrip('/')+path, headers={'Accept': 'application/json'})
            with opener(request, timeout=3) as response:
                data = json.loads(response.read(1_000_001))
            if not isinstance(data, dict):
                raise ValueError('Invalid response')
            if name == 'Fleet':
                ready = not data.get('errors') and data.get('available', {}).get('health') is True
                detail = 'read-only overview reachable' if ready else 'overview incomplete; try /fleet'
            else:
                ready = data.get('ready') is True
                detail = 'ready' if ready else 'reachable, not ready'
            checks.append({'name': name, 'status': 'ok' if ready else 'attention', 'detail': detail})
        except (OSError, ValueError, urllib.error.URLError):
            checks.append({'name': name, 'status': 'unavailable', 'detail': 'No valid health response; check the service or tunnel.'})
    return checks


def format_checks(checks):
    return '\n'.join(f"{item['name']}: {item['status']} · {item['detail']}" for item in checks)
