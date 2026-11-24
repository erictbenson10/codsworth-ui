"""Presentation for bounded read-only Fleet snapshots; no transport or controls."""
from datetime import datetime


def overview_text(snapshot, questions_only=False):
    available = snapshot.get('available', {})
    observed = str(snapshot.get('observed_at', 'just now'))
    try:
        observed = datetime.fromisoformat(observed).astimezone().strftime('%b %d, %H:%M:%S %Z')
    except ValueError:
        pass
    lines = ['Checked '+observed]
    if not questions_only:
        if available.get('health'):
            health = snapshot.get('health', {})
            admissions = {True: 'paused', False: 'enabled'}.get(health.get('paused'), 'unknown')
            lines.append('Daemon: '+str(health.get('daemon', 'unknown'))+
                         ' · admissions '+admissions)
        else:
            lines.append('Daemon health unavailable')
        if available.get('status'):
            counts = snapshot.get('counts', {})
            running = counts.get('running') if counts.get('running') is not None else '?'
            queued = counts.get('queued') if counts.get('queued') is not None else '?'
            lines.append(f'Running: {running} · Queued: {queued}')
        if available.get('attempts'):
            for attempt in snapshot.get('attempts', [])[:10]:
                lines.append(f"{attempt.get('id', '?')} · {attempt.get('state', '?')} · {attempt.get('name') or 'Untitled attempt'}")
        lines.append('')
    if not available.get('questions'):
        lines.append('Questions unavailable — this does not mean the inbox is empty. Try /questions again.')
    elif not snapshot.get('questions'):
        if any('question' in str(error).lower() for error in snapshot.get('errors', [])):
            lines.append('Question data is incomplete; inspect Fleet before assuming the inbox is empty.')
        else:
            lines.append('No open Fleet questions.')
    else:
        lines.append('Fleet needs your input:')
        for question in snapshot['questions'][:20]:
            lines.append(f"\n[{question.get('id', '?')}] {question.get('text', '')}")
            if question.get('attempt_id'):
                lines.append('Attempt: '+str(question['attempt_id']))
            for i, option in enumerate(question.get('options', [])[:8], 1):
                lines.append(f'{i}. {option}')
            if question.get('options_truncated'):
                lines.append('Options abbreviated; ask for the full question before answering.')
        lines.append('\nReply naturally: Answer Fleet question <ID> with <your answer>.')
    truncated = snapshot.get('truncated', False)
    has_more = any(truncated.values()) if isinstance(truncated, dict) else bool(truncated)
    if has_more:
        lines.append('More results exist; ask Codsworth for the specific run or question.')
    if snapshot.get('errors'):
        lines.append('\nSome Fleet data could not be refreshed. Try /fleet again.')
    return '\n'.join(lines)
