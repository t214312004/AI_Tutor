"""Application-owned request identity; agent messages remain free-form text."""
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from copy import deepcopy
import time
from uuid import uuid4

CURRENT_WORK = ContextVar('classroom_work', default=None)


class LessonWork:
    def __init__(self, session, request, source='internal', source_id=None, timeout=150):
        session.work_sequence += 1
        self.session = session
        self.sequence = session.work_sequence
        self.id = uuid4().hex
        self.request, self.source, self.source_id = request, source, source_id
        self.speaker = session.input_speaker
        self.generation = session.generation
        self.plan_revision = session.plan_revision
        self.direction_revision = session.direction_revision
        self.history = deepcopy(session.heard_history[-30:])
        self.deadline = time.monotonic() + timeout
        self.observation = None
        self.photo_ref = None
        self._staged = {}
        self._locks = set()
        self.committed = False

    def valid(self):
        s = self.session
        return (s.active and self.generation == s.generation
                and self.plan_revision == s.plan_revision
                and self.direction_revision == s.direction_revision
                and self.sequence >= s.committed_sequence
                and time.monotonic() <= self.deadline)

    def remaining(self):
        return max(0, self.deadline - time.monotonic())

    def stage(self, key, operation, undo=None, lock=None, file=False):
        if not self.valid():
            return False
        self._staged[key] = (operation,undo,file)
        if lock:self._locks.add(lock)
        return True

    @contextmanager
    def bind(self):
        token = CURRENT_WORK.set(self)
        try:
            yield self
        finally:
            CURRENT_WORK.reset(token)

    def commit(self):
        if not self.valid():
            self.discard()
            return False
        # There is no await between validation, sequence adoption and publication.
        previous_sequence=self.session.committed_sequence
        token = CURRENT_WORK.set(None)
        applied=[]
        try:
            with ExitStack() as stack:
                for lock in sorted(self._locks,key=id):stack.enter_context(lock)
                self.session.committed_sequence = self.sequence
                try:
                    for operation,undo,_ in sorted(self._staged.values(),key=lambda value:not value[2]):
                        applied.append(undo);operation()
                except BaseException:
                    self.session.committed_sequence=previous_sequence
                    for undo in reversed(applied):
                        if undo:
                            try:undo()
                            except Exception as error:
                                from .diagnostics import report_failure
                                report_failure('publication_rollback',error)
                    raise
        finally:
            CURRENT_WORK.reset(token)
            self._staged.clear()
            self._locks.clear()
        self.committed = True
        return True

    def rebase_direction(self):
        self.direction_revision = self.session.direction_revision

    def discard(self):
        self._staged.clear()
        self._locks.clear()

    def stage_file(self,key,path,operation,lock=None,after_undo=None):
        backup={}
        def apply():
            backup['previous']=path.read_bytes() if path.exists() else None
            operation()
        def undo():
            if 'previous' not in backup:return
            previous=backup['previous']
            from .collaboration_cli import atomic_text
            if previous is None:path.unlink(missing_ok=True)
            else:atomic_text(path,previous.decode('utf-8'))
            if after_undo:after_undo()
        return self.stage(key,apply,undo=undo,lock=lock,file=True)


def applicable_observation(session, max_age=30):
    """One source for prompt, curriculum and tools; history is explicitly separate."""
    work = CURRENT_WORK.get()
    observation = work.observation if work and work.observation else session.observation
    if not session.camera_enabled or not observation:
        return None
    if observation.get('withdrawn') or time.monotonic() - observation.get('received', 0) > max_age:
        return None
    if observation.get('historical') and not (work and work.observation is observation):
        return None
    if observation.get('generation', session.generation) != session.generation:
        return None
    if observation.get('plan_revision', session.plan_revision) != session.plan_revision:
        return None
    return observation
