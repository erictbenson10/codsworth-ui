"""Discoverable terminal commands; completion never executes a command."""
COLOR_OPTIONS = ('green','cyan','amber','blue','purple','white')
EFFORT_LEVELS = ('none','low','medium','high')
COMMANDS = (
    ('/help', 'Keyboard and command help'),
    ('/speed', 'Adjust speech speed'),
    ('/effort', 'Set reasoning effort'),
    ('/voice', 'Choose expanded or pilot voice'),
    ('/mute', 'Toggle spoken replies'),
    ('/humor', 'Set humor from 0 to 1'),
    ('/sarcasm', 'Set sarcasm from 0 to 1'),
    ('/tasks', 'List tasks and their status'),
    ('/task', 'Manage task IDs, priorities and due dates'),
    ('/todo', 'Capture a local task'),
    ('/todos', 'Show captured tasks'),
    ('/done', 'Complete a task by number'),
    ('/files', 'List local workspace files'),
    ('/plugins', 'Tools and connection status'),
    ('/fleet', 'Fleet workspace tools'),
    ('/questions', 'Review pending questions'),
    ('/copy', 'Copy conversation'),
    ('/export', 'Export conversation to a new file'),
    ('/tray', 'Open the tray on a view'),
    ('/streams', 'Manage concurrent workstreams'),
    ('/stream', 'Create or stop an independent workstream'),
    ('/focus', 'Choose workstream or home message target'),
    ('/sessions', 'List saved conversations'),
    ('/switch', 'Switch to a conversation ID'),
    ('/rename', 'Rename this conversation'),
    ('/color', 'Choose this conversation’s accent color'),
    ('/watch', 'Watch a workspace operation'),
    ('/updates', 'Review operation updates'),
    ('/motion', 'Choose full or reduced animation'),
    ('/resume', 'Resume a saved workspace'),
    ('/new', 'Start a fresh conversation'),
    ('/links', 'List web links in this conversation'),
    ('/open', 'Open a numbered link from /links'),
    ('/quit', 'Close this terminal'),
    ('/bug', 'Report a Codsworth bug'),
    ('/inbox', 'Corner work: Gitea issues, PRs and Fleet'),
    ('/issues', 'Browse Corner issues in Gitea'),
    ('/bugs', 'Browse Codsworth maintenance issues'),
    ('/tickets', 'Alias for /issues: Corner Gitea issues'),
    ('/doctor', 'Check services and local audio tools'),
    ('/settings', 'Current speech, motion and watch settings'),
    ('/joke', 'A little wasteland wit'),
    ('/tea', 'A virtual cuppa'),
    ('/vault', 'Vault-Tec productivity orientation'),
)


def matches(draft, sessions=(), tasks=()):
    if not draft.startswith('/') or '\n' in draft or '\t' in draft:
        return []
    if ' ' not in draft:
        return [(name, description) for name, description in COMMANDS if name.startswith(draft.lower())]
    command,tail=draft.split(' ',1)
    enums={'/color':COLOR_OPTIONS,'/effort':EFFORT_LEVELS,'/voice':('expanded','pilot'),'/motion':('full','reduced'),'/watch':('on','off'),'/tasks':('open','done','all','archived'),'/tickets':('open','closed','all'),'/issues':('open','closed','all'),'/bugs':('open','closed','all'),
           '/tray':('glance','fleet','inbox','streams','tasks','doctor')}
    if command in enums and ' ' not in tail:
        return [(command+' '+item,'Use '+item) for item in enums[command] if item.startswith(tail)]
    if command == '/switch' and ' ' not in tail:
        return [('/switch '+item['conversation_id'],item.get('title','Untitled')) for item in sessions if item['conversation_id'].startswith(tail)][:50]
    if command == '/task':
        operations=('done','reopen','priority','due','rename','archive','restore')
        operation,sep,prefix=tail.partition(' ')
        if not sep:
            return [('/task '+item,'Choose task for '+item) for item in operations if item.startswith(operation)]
        if operation in operations:
            candidates = [item for item in tasks if bool(item.get('archived')) == (operation == 'restore')]
            if operation == 'archive':
                candidates = [item for item in candidates if item.get('status') == 'done']
            return [('/task '+operation+' '+item['id'],item['title']) for item in candidates if item['id'].startswith(prefix.lower()) or (len(prefix) >= 2 and prefix.casefold() in item['title'].casefold())][:50]
    return []


def session_choices(directory):
    """Bounded metadata read, called only when entering/refreshing switch completion."""
    import json
    from itertools import islice
    result=[]
    budget=8_000_000
    for path in islice(directory.glob('*.json'),50):
        try:
            size=path.stat().st_size
            if size>1_000_000 or size>budget:
                continue
            budget-=size
            data=json.loads(path.read_text())
            if data.get('conversation_id') == path.stem:
                result.append({'conversation_id':path.stem,'title':str(data.get('title','Untitled'))[:80]})
        except (OSError,ValueError,AttributeError):
            continue
    return result
