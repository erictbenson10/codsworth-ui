"""Requested work -> Gitea issue -> Fleet run, with durable no-retry receipts."""
import fcntl
from functools import lru_cache
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re

from .connector import FleetClient


@lru_cache(maxsize=1)
def gitea_connector():
    path = Path(__file__).resolve().parents[1] / 'cods-gitea' / 'connector.py'
    spec = importlib.util.spec_from_file_location('_cods_fleet_gitea', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plain(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(
            (ord(c) < 32 and c not in '\n\t') or 127 <= ord(c) <= 159 for c in value):
        raise ValueError(f'{name} must be nonempty plain text, at most {limit} characters.')
    return value.strip()


def plan(args):
    allowed = {'request_id', 'kind', 'repo', 'issue', 'title', 'description', 'acceptance_criteria'}
    if not isinstance(args, dict) or set(args) - allowed:
        raise ValueError('Unsupported task arguments.')
    request_id = args.get('request_id')
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', request_id):
        raise ValueError('Use a stable request_id of 8–80 letters, digits, underscores or hyphens for this task.')
    kind = args.get('kind', 'work')
    if kind not in ('work', 'self_improvement'):
        raise ValueError('Task kind must be work or self_improvement.')
    repo = args.get('repo', 'example/codsworth' if kind == 'self_improvement' else 'example/workspace')
    if kind == 'self_improvement' and repo != 'example/codsworth':
        raise ValueError('Self-improvement tasks belong in example/codsworth.')
    # Gitea's configured allowlist is the same boundary used by its native tools.
    if repo not in os.environ.get('CODS_GITEA_REPOS', 'example/workspace,example/codsworth,example/fleet').split(','):
        raise ValueError('Repository is outside the configured allowlist.')
    if 'issue' in args:
        if type(args['issue']) is not int or args['issue'] < 1:
            raise ValueError('Use an existing positive issue number.')
        if set(args) & {'title', 'description', 'acceptance_criteria'}:
            raise ValueError('Provide either an existing issue or a new task description, not both.')
        issue_args = dict(action='get', repo=repo, issue=args['issue'])
    else:
        title = plain(args.get('title'), 'Title', 160)
        description = plain(args.get('description'), 'Description', 6000)
        acceptance = plain(args.get('acceptance_criteria'), 'Acceptance criteria', 3000)
        body = (description + '\n\n## Acceptance criteria\n\n' + acceptance +
                '\n\n## Verification\n\nAdd regression tests for behavior changes. Run relevant checks and report the results and PR link. '
                'Do not lower coverage thresholds, skip valid tests or weaken assertions. Preserve unrelated work.')
        if kind == 'self_improvement':
            body += (' Improve Codsworth in the versioned example/codsworth repository. Preserve voice/model assets and conversation compatibility. '
                     'Use Fleet\'s normal review, CI and merge policy; do not patch the running assistant or change service permissions to bypass review.')
        body += '\n\n<!-- codsworth-task:' + request_id + ' -->'
        issue_args = dict(action='create', repo=repo, title=title, body=body)
    return request_id, repo, issue_args


def dispatch(args, *, fleet=None, gitea=None, directory=None):
    """A repeated request returns its receipt, including uncertain outcomes; never replays writes."""
    request_id, repo, issue_args = plan(args)
    fingerprint = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
    directory = Path(directory) if directory else Path(os.environ.get('HERMES_HOME', str(Path.home()/'.hermes'))) / 'fleet-tasks'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / (request_id + '.json')
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, 'r+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(ok=False, request_id=request_id, phase='busy', retryable=False,
                        error='This task submission is in progress. Read its receipt later with the same request_id.')
        previous = handle.read(24001)
        if previous:
            record = json.loads(previous)
            if record.get('fingerprint') != fingerprint:
                raise ValueError('That request_id belongs to a different task. Use its original arguments to read the receipt.')
            return dict(record['receipt'], replayed=True)

        receipt = dict(ok=False, request_id=request_id, repo=repo, retryable=False, phase='preparing')
        def save(**changes):
            receipt.update(changes)
            handle.seek(0)
            json.dump(dict(fingerprint=fingerprint, receipt=receipt), handle)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())

        try:
            client = fleet or FleetClient()
            invoke = gitea or gitea_connector().invoke
            # Resolve both connectors before the first write. A paused Fleet can
            # still accept queued work, so health does not require active admissions.
            health = client.read({'view':'health'})
            if health.get('ok') is not True:
                save(phase='unavailable', error='Fleet is unavailable; no issue or run was created.')
                return receipt
            creating = issue_args['action'] == 'create'
            save(phase='creating_issue' if creating else 'reading_issue', outcome='unknown',
                 error='Submission was interrupted. Inspect the issue and Fleet before attempting another write.')
            issue_result = json.loads(invoke('write' if creating else 'read', issue_args))
            if issue_result.get('success') is not True:
                code = issue_result.get('http_status')
                rejected = type(code) is int and 400 <= code < 500
                save(phase='issue_failed', outcome='rejected' if rejected else 'unknown',
                     error='Gitea did not return an issue receipt. Inspect Gitea before resubmitting; Fleet was not queued.')
                return receipt
            data = issue_result.get('data') or {}
            number, url = data.get('number'), data.get('html_url')
            urls = (f'https://git.example.com/{repo}/issues/{number}', f'http://192.0.2.25:3080/{repo}/issues/{number}')
            if type(number) is not int or number < 1 or url not in urls or data.get('pull_request') or (
                    not creating and number != issue_args['issue']):
                save(phase='issue_failed', outcome='unknown', error='Gitea returned an invalid issue receipt; Fleet was not queued.')
                return receipt
            save(issue=number, issue_url=urls[0], phase='issue_created' if creating else 'issue_found', outcome='confirmed')
            if data.get('state') != 'open':
                save(phase='issue_closed', error='The issue is not open. Reopen it explicitly before queueing.')
                return receipt
            save(phase='queueing', outcome='unknown', error='Fleet receipt is missing. Inspect Fleet before retrying this issue.')
            result = client.control({'action':'run', 'repo':repo, 'issue':number})
            run = result.get('data') or {}
            if (result.get('ok') is True and run.get('ok') is True and run.get('queued') is True
                    and run.get('repo') == repo and type(run.get('issue')) is int and run['issue'] == number
                    and isinstance(run.get('run'), str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', run['run'])
                    and isinstance(run.get('attempt'), str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', run['attempt'])):
                save(ok=True, phase='queued', outcome='confirmed', run_id=run['run'], attempt_id=run['attempt'],
                     error=None, notice='Queued for Fleet admission. This is not evidence of implementation, passing CI or deployment.')
            else:
                status = result.get('status')
                save(phase='queue_failed', outcome='rejected' if type(status) is int and 400 <= status < 500 else 'unknown',
                     error='Issue is saved, but Fleet queueing was not confirmed. Inspect Fleet; reuse this issue rather than creating another.')
            return receipt
        except Exception:
            # Keep known issue IDs and a durable receipt even when the network,
            # connector, or response decoder fails between the two writes.
            save(error='Task submission did not finish. Inspect the saved issue/Fleet receipt before any new write.')
            return receipt


def invoke_task(args):
    try:
        return json.dumps(dispatch(args), ensure_ascii=False)
    except (ValueError, OSError, TypeError):
        return json.dumps(dict(ok=False, retryable=False, error='Invalid task arguments or unavailable private receipt store. Check the schema and reuse the original request_id to inspect prior submissions.'))
