"""Bounded Fleet HTTP adapter. No shell commands or automatic write retries."""
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class FleetClient:
    def __init__(self, url=None, token_file=None, opener=None):
        self.url = (url if url is not None else os.environ.get('CODS_FLEET_URL', '')).rstrip('/')
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise ValueError('Fleet endpoint is not configured correctly')
        self.token_file = token_file or os.environ.get('CODS_FLEET_TOKEN_FILE', '')
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def request(self, method, path, body=None):
        headers = {'Accept': 'application/json'}
        token = ''
        if method == 'POST':
            if not self.token_file:
                raise ValueError('Fleet control credential is not configured')
            token = Path(self.token_file).read_text().strip()
            if not token or '\n' in token or '\r' in token:
                raise ValueError('Fleet control credential is invalid')
            headers.update(Authorization='Bearer '+token, **{'Content-Type': 'application/json'})
        payload = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url+path, data=payload, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=12) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError('Fleet response exceeds the size limit; narrow the query')
            return {'ok': True, 'data': json.loads(raw)}
        except urllib.error.HTTPError as error:
            # Never forward HTML, headers, bearer tokens or arbitrary remote error bodies.
            error.close()
            return {'ok': False, 'status': error.code, 'error': {
                401: 'Fleet rejected the control credential',
                403: 'Fleet denied this operation',
                404: 'Fleet target was not found',
                409: 'Fleet reports a conflict; inspect the current run or question before retrying'
            }.get(error.code, 'Fleet request failed'), 'retryable': False}
        except (TimeoutError, OSError, urllib.error.URLError):
            return {'ok': False, 'error': 'Fleet connection failed',
                    'outcome': 'unknown; inspect Fleet before retrying this action' if method == 'POST' else 'unavailable',
                    'retryable': method == 'GET'}

    @staticmethod
    def identifier(value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', value) or value in ('.', '..'):
            raise ValueError('Use the exact Fleet ID returned by a read operation')
        return urllib.parse.quote(value, safe='')

    def read(self, args):
        view = args.get('view', 'status')
        paths = {'health': '/healthz', 'status': '/api/status', 'questions': '/api/questions',
                 'attempts': '/api/attempts'}
        if view in paths:
            path = paths[view]
        elif view == 'attempt':
            path = '/api/attempts/'+self.identifier(args.get('id'))
        elif view == 'run':
            path = '/api/runs/'+self.identifier(args.get('id'))
        elif view == 'events':
            limit = args.get('limit', 30)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError('Event limit must be an integer from 1 to 100')
            query = {'limit': limit}
            since = args.get('since')
            if since is not None:
                if not isinstance(since, str) or len(since) > 64:
                    raise ValueError('Event since must be a timezone-aware ISO timestamp')
                parsed = datetime.fromisoformat(since.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    raise ValueError('Event since must include a timezone')
                query['since'] = parsed.astimezone(timezone.utc).isoformat()
            path = '/api/events?' + urllib.parse.urlencode(query)
        else:
            raise ValueError('Unknown Fleet view')
        return self.request('GET', path)

    def control(self, args):
        action = args.get('action')
        body = {'by': 'codsworth'}
        if action in ('pause', 'resume'):
            path = '/api/'+action
        elif action == 'run':
            repo, issue = args.get('repo'), args.get('issue')
            if not isinstance(repo, str) or not re.fullmatch(r'[\w.-]+/[\w.-]+', repo) or type(issue) is not int or issue < 1:
                raise ValueError('Run requires an owner/repo and a positive issue number')
            body.update(repo=repo, issue=issue)
            path = '/api/runs'
        elif action in ('answer', 'say', 'eject'):
            target = self.identifier(args.get('id'))
            text = args.get('text', '')
            if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                raise ValueError('Provide 1–4000 characters of answer, instruction or reason')
            key = {'answer': 'answer', 'say': 'text', 'eject': 'reason'}[action]
            body[key] = text.strip()
            path = ('/api/questions/' if action == 'answer' else '/api/attempts/')+target+'/'+action
        else:
            raise ValueError('Unknown Fleet action')
        return self.request('POST', path, body)


def invoke(kind, args):
    try:
        result = getattr(FleetClient(), kind)(args)
        return json.dumps(result, ensure_ascii=False)
    except (ValueError, OSError, TypeError):
        # Configuration errors must not reveal local paths or credential contents.
        return json.dumps({'ok': False, 'error': 'Invalid Fleet arguments or unavailable connector configuration. Check the tool schema and private Fleet configuration.'})
