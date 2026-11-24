"""Private workspace conversation snapshots; active workspace file stays compatible."""
import json
import os
from pathlib import Path
import re
import tempfile


class ConversationStore:
    def __init__(self, workspace_path):
        path = Path(workspace_path)
        self.directory = path.parent / 'conversations' / path.stem

    def save(self, snapshot):
        ident = snapshot['conversation_id']
        if not re.fullmatch('[0-9a-f]{32}', ident):
            raise ValueError('Invalid conversation identifier')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix='.conversation-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(snapshot, stream, indent=2)
                stream.write('\n')
            os.replace(temporary, self.directory / (ident+'.json'))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def entries(self):
        result = []
        if self.directory.exists():
            for path in self.directory.glob('*.json'):
                try:
                    data = json.loads(path.read_text())
                    if data.get('conversation_id') == path.stem:
                        result.append(data)
                except (OSError, ValueError, AttributeError):
                    continue
        return sorted(result, key=lambda item: item.get('updated_at', 0), reverse=True)

    def load(self, prefix):
        if not re.fullmatch('[0-9a-f]{4,32}', prefix):
            raise ValueError('Use /switch followed by an ID from /sessions')
        matches = [item for item in self.entries() if item['conversation_id'].startswith(prefix)]
        if len(matches) != 1:
            raise ValueError('Conversation ID is missing or ambiguous. Use /sessions for IDs.')
        return matches[0]
