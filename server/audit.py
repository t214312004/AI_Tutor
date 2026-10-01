"""Per-session evidence for reviewing the requests sent to tutoring backends."""
import base64
import hashlib
import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

RETENTION_SECONDS = 72 * 60 * 60


def _is_link(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _generated_agy_log(name):
    token = name[4:-4]
    return (name.startswith('agy-') and name.endswith('.log') and len(token) == 32 and
            all(char in '0123456789abcdef' for char in token))


def _cleanup_root(root):
    """Refuse cleanup through a symlink or Windows junction at any path component."""
    root = Path(root).absolute()
    for part in (root, *root.parents):
        if _is_link(part):
            return None
    return root.resolve() if root.is_dir() else None


def prune_audits(root: Path, active_ids=(), now=None):
    """Remove finished test-session evidence 72 hours after its last log entry."""
    root = _cleanup_root(root)
    if root is None:
        return []
    cutoff = (time.time() if now is None else now) - RETENTION_SECONDS
    active_ids = set(active_ids)
    removed = []
    for folder in root.iterdir():
        if (folder.name in active_ids or len(folder.name) != 32 or
                any(char not in '0123456789abcdef' for char in folder.name) or
                _is_link(folder) or not folder.is_dir() or
                not folder.resolve().is_relative_to(root)):
            continue
        lesson_log = folder / 'lesson.jsonl'
        if not lesson_log.exists():
            lesson_log = folder / 'agent-turns.jsonl'
        try:
            modified = lesson_log.stat().st_mtime if lesson_log.is_file() else folder.stat().st_mtime
            if modified <= cutoff:
                shutil.rmtree(folder)
                removed.append(folder.name)
        except OSError as error:
            logging.getLogger('tutor.audit').warning('audit_prune_failed folder=%s type=%s',
                                                     folder.name, type(error).__name__)
    return removed


def prune_provider_logs(root: Path, now=None):
    """Expire only app-generated agy log files, including older shared workspaces."""
    root = _cleanup_root(root)
    if root is None:
        return 0
    cutoff = (time.time() if now is None else now) - RETENTION_SECONDS
    removed = 0
    for directory, subdirs, files in os.walk(root, followlinks=False):
        subdirs[:] = [name for name in subdirs if name != 'photos' and
                      not _is_link(Path(directory) / name)]
        for name in files:
            if not _generated_agy_log(name):continue
            path = Path(directory) / name
            try:
                if (_is_link(path) or not path.is_file() or
                        not path.resolve().is_relative_to(root) or path.stat().st_mtime > cutoff):
                    continue
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


class AuditDiagnosticHandler(logging.Handler):
    def __init__(self, audit):
        super().__init__(logging.INFO)
        self.audit = audit

    def emit(self, record):
        try:
            self.audit.record('core_diagnostic', level=record.levelname,
                              logger=record.name, message=record.getMessage())
        except Exception:
            self.handleError(record)


class SessionAudit:
    def __init__(self, root: Path, session_id: str, mode: str, backend: str, workspace: Path):
        base = Path(root).absolute()
        if any(_is_link(part) for part in (base, *base.parents)):
            raise OSError('Test-session audit path contains a link')
        base.mkdir(parents=True, exist_ok=True)
        self.root = base / session_id
        self.root.mkdir(exist_ok=False)
        self.events = self.root / 'lesson.jsonl'
        self.sequence = 0
        self.lock = threading.Lock()
        self.workspace = Path(workspace)
        self.diagnostic_handler = None
        self.finished = False
        if self.record('session_started', mode=mode, backend=backend, workspace=str(workspace)) is None:
            raise OSError('Cannot create test-session audit log')
        self.capture_guide()

    @classmethod
    def resume(cls, root: Path, session_id: str):
        root = _cleanup_root(root)
        if (root is None or len(session_id) != 32 or
                any(char not in '0123456789abcdef' for char in session_id)):
            return None
        folder = root / session_id
        events = folder / 'lesson.jsonl'
        if (_is_link(folder) or not folder.is_dir() or
                not folder.resolve().is_relative_to(root) or _is_link(events) or not events.is_file() or
                not events.resolve().is_relative_to(folder)):
            return None
        with events.open('rb+') as stream:
            stream.seek(0, os.SEEK_END)
            end = stream.tell()
            if end:
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b'\n':
                    position = end
                    start = 0
                    while position:
                        count = min(position, 65536)
                        position -= count
                        stream.seek(position)
                        index = stream.read(count).rfind(b'\n')
                        if index >= 0:
                            start = position + index + 1
                            break
                    stream.seek(start)
                    try:json.loads(stream.read(end - start).decode('utf-8'))
                    except (json.JSONDecodeError, UnicodeDecodeError):stream.truncate(start)
                    else:
                        stream.seek(end)
                        stream.write(b'\n')
        first = None
        sequence = 0
        with events.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:item = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):continue  # A process can stop during a write.
                if not isinstance(item, dict):continue
                if first is None and item.get('kind') == 'session_started':first = item
                number = item.get('sequence')
                if isinstance(number, int):sequence = max(sequence, number)
        if not first or first.get('kind') != 'session_started' or not first.get('workspace'):
            return None
        audit = cls.__new__(cls)
        audit.root = folder
        audit.events = events
        audit.sequence = sequence
        audit.lock = threading.Lock()
        audit.workspace = Path(first['workspace'])
        audit.diagnostic_handler = None
        audit.finished = False
        audit.record('session_resumed')
        return audit

    def capture_guide(self):
        guide = self.workspace / 'AGENTS.md'
        if guide.is_file():
            (self.root / 'AGENTS.md').write_bytes(guide.read_bytes())

    def attach_diagnostics(self):
        if self.diagnostic_handler is None:
            self.diagnostic_handler = AuditDiagnosticHandler(self)
            logging.getLogger('tutor.transport').addHandler(self.diagnostic_handler)

    def finish(self):
        if self.finished:
            return
        try:self.record('session_closed')
        finally:
            self.finished = True
            if self.diagnostic_handler is not None:
                logging.getLogger('tutor.transport').removeHandler(self.diagnostic_handler)
                self.diagnostic_handler.close()
                self.diagnostic_handler = None

    def record(self, kind: str, **details):
        with self.lock:
            sequence = self.sequence + 1
            item = {'sequence': sequence, 'at': datetime.now(timezone.utc).isoformat(),
                    'kind': kind, **details}
            encoded = json.dumps(item, ensure_ascii=False) + '\n'
            try:
                with self.events.open('a', encoding='utf-8') as stream:
                    stream.write(encoded)
            except OSError as error:
                logging.getLogger('tutor.audit').warning('audit_write_failed session=%s type=%s',
                                                         self.root.name, type(error).__name__)
                return None
            self.sequence = sequence
            return sequence

    def save_live_photo(self, capture_id: str, image_url: str, captured_at: str):
        try:
            image = base64.b64decode(image_url.split(',', 1)[1], validate=True)
            photos = self.root / 'photos'
            photos.mkdir(exist_ok=True)
            suffix = 'png' if image_url.startswith('data:image/png;base64,') else 'jpg'
            path = photos / f'{capture_id}.{suffix}'
            path.write_bytes(image)
            self.record('photo_saved', capture_id=capture_id, captured_at=captured_at,
                        path=f'photos/{path.name}', sha256=hashlib.sha256(image).hexdigest())
        except OSError as error:
            logging.getLogger('tutor.audit').warning('photo_archive_failed session=%s type=%s',
                                                     self.root.name, type(error).__name__)

    def snapshot_workspace(self, source=None, folder='workspace-after'):
        """Keep the end-of-lesson notes even when the shared workspace changes later."""
        try:
            workspace = Path(source) if source else self.workspace
            destination = self.root / folder
            if any(_is_link(part) for part in (workspace, *workspace.parents)) or _is_link(destination):
                raise OSError('Workspace snapshot path contains a link')
            def raise_walk_error(error):raise error
            for directory, subdirs, files in os.walk(workspace, followlinks=False,onerror=raise_walk_error):
                relative_dir = Path(directory).relative_to(workspace)
                subdirs[:] = [name for name in subdirs if not _is_link(Path(directory) / name)
                              and not (source is None and relative_dir == Path('internal') and name == 'photos')]
                for name in files:
                    entry = Path(directory) / name
                    if _is_link(entry) or _generated_agy_log(name):continue
                    relative = entry.relative_to(workspace)
                    if source is None and (relative.parts[:2] == ('internal', 'photos') or
                                           relative == Path('internal/latest-photo.jpg')):
                        continue  # The Basic photo archive remains in the shared workspace.
                    target = destination / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(entry, target)
            self.record('workspace_snapshot', path=folder)
        except OSError as error:
            self.record('workspace_snapshot_failed', path=folder,error_type=type(error).__name__)
            logging.getLogger('tutor.audit').warning('workspace_snapshot_failed session=%s type=%s',
                                                     self.root.name, type(error).__name__)


class AuditedProvider:
    def __init__(self, provider, audit: SessionAudit, label: str):
        self.provider = provider
        self.audit = audit
        self.label = label
        self._attach(provider)

    def _attach(self, provider):
        provider.audit = self.audit
        if hasattr(provider, 'client'):
            provider.client.audit = self.audit
        for backend in getattr(provider, 'backends', {}).values():
            self._attach(backend)

    def __getattr__(self, name):
        return getattr(self.provider, name)

    async def turn(self, prompt, *, image_url=None, schema=None, **kwargs):
        started = time.monotonic()
        image_sha256 = (hashlib.sha256(base64.b64decode(image_url.split(',', 1)[1])).hexdigest()
                        if image_url else None)
        turn_id = self.audit.record('agent_request', provider=self.label,
                                    model=getattr(self.provider,'model',None),
                                    effort=getattr(self.provider,'effort',None),
                                    prompt=prompt, schema=schema, image_sha256=image_sha256)
        try:
            response = await self.provider.turn(prompt, image_url=image_url, schema=schema, **kwargs)
        except BaseException as error:
            self.audit.record('agent_error', turn_id=turn_id, error_type=type(error).__name__,
                              elapsed_ms=round((time.monotonic()-started)*1000))
            raise
        self.audit.record('agent_response', turn_id=turn_id, response=response,
                          elapsed_ms=round((time.monotonic()-started)*1000),
                          effective_backend=getattr(self.provider, 'active', self.label),
                          conversation_id=getattr(self.provider, 'conversation_id', None),
                          thread_id=getattr(getattr(self.provider, 'client', None), 'thread_id', None))
        return response

    async def close(self):
        return await self.provider.close()
