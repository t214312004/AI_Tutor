import asyncio
import base64
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from .audit import AuditedProvider, SessionAudit, prune_audits, prune_provider_logs, RETENTION_SECONDS
from .basic import BasicRuntime
from .basic_workspace import BasicWorkspace
from .test_basic import Speech
from .test_basic_workspace import RecordingTutor
from .test_live_runtime import Provider
from .tutor import TutorSession
from .live_runtime import LiveRuntime


class AuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_basic_lesson_keeps_prompts_responses_photos_and_notes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = BasicWorkspace(root / 'teacher')
            audit = SessionAudit(root / 'audit', 'lesson-1', 'basic', 'codex', workspace.root)
            provider = AuditedProvider(RecordingTutor(), audit, 'teacher')
            session = TutorSession('student-test', 3, basic_workflow=True)
            async def emit(event): pass
            runtime = BasicRuntime(session, provider, provider, Speech(), emit, lambda *args: None, workspace)
            runtime.audit = audit
            image = b'\xff\xd8\xfftest-photo'
            runtime.accept_frame('data:image/jpeg;base64,' + base64.b64encode(image).decode())
            await runtime.observations.task
            (workspace.root / 'notes.txt').write_text('下次要複習進位', encoding='utf-8')
            runtime.accept_text('請教我進位')
            await asyncio.gather(*runtime.voice_tasks)
            await runtime.close()
            events = [json.loads(line) for line in audit.events.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(audit.events.name, 'lesson.jsonl')
            requests = [item for item in events if item['kind'] == 'agent_request']
            self.assertTrue(any('請教我進位' in item['prompt'] for item in requests))
            self.assertTrue(any(item['kind'] == 'agent_response' for item in events))
            self.assertEqual(len([item for item in events if item['kind'] == 'basic_photo_saved']), 1)
            self.assertEqual(len(list(workspace.photos.glob('*.jpg'))), 1)
            self.assertEqual((audit.root / 'workspace-after' / 'notes.txt').read_text(encoding='utf-8'), '下次要複習進位')
            self.assertEqual((audit.root / 'AGENTS.md').read_bytes(), (workspace.root / 'AGENTS.md').read_bytes())

    async def test_live_photo_is_saved_for_later_review(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / 'teacher'
            workspace.mkdir()
            audit = SessionAudit(root / 'audit', 'lesson-2', 'gemini', 'gemini', workspace)
            provider = AuditedProvider(RecordingTutor(), audit, 'teacher')
            async def emit(event): pass
            runtime = LiveRuntime(TutorSession('student-test', 3), provider, provider,
                                  'gemini', 'test-key', emit, lambda *args: None)
            runtime.provider = Provider()
            runtime.audit = audit
            image = b'\xff\xd8\xfflive-photo'
            runtime.accept_frame('data:image/jpeg;base64,' + base64.b64encode(image).decode())
            await runtime.observations.task
            await asyncio.gather(*list(runtime.photo_tasks))
            await runtime.close()
            events = [json.loads(line) for line in audit.events.read_text(encoding='utf-8').splitlines()]
            saved = [item for item in events if item['kind'] == 'photo_saved']
            self.assertEqual(len(saved), 1)
            self.assertEqual((audit.root / saved[0]['path']).read_bytes(), image)
            self.assertTrue(any(item['kind'] == 'live_image_sent' and
                                item['capture_id'] == saved[0]['capture_id'] for item in events))

    async def test_live_workspace_snapshot_includes_teacher_and_observer(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / 'live-session'
            for part in ('teacher', 'observer'):
                target = workspace / part / 'notes.txt'
                target.parent.mkdir(parents=True)
                target.write_text(part, encoding='utf-8')
            (workspace / 'teacher' / ('agy-' + 'a' * 32 + '.log')).write_text('old log', encoding='utf-8')
            outside = root / 'outside'
            outside.mkdir()
            (outside / 'private.txt').write_text('outside', encoding='utf-8')
            try:(workspace / 'observer' / 'linked').symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):pass
            audit = SessionAudit(root / 'audit', 'd' * 32, 'gpt', 'agy', workspace)
            audit.snapshot_workspace()
            for part in ('teacher', 'observer'):
                self.assertEqual((audit.root / 'workspace-after' / part / 'notes.txt').read_text(encoding='utf-8'), part)
            self.assertFalse((audit.root / 'workspace-after' / 'observer' / 'linked' / 'private.txt').exists())
            self.assertFalse((audit.root / 'workspace-after' / 'teacher' / ('agy-' + 'a' * 32 + '.log')).exists())

    async def test_live_tool_exchange_is_recorded_and_unsent_image_is_not(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / 'teacher'
            workspace.mkdir()
            audit = SessionAudit(root / 'audit', '1' * 32, 'gemini', 'gemini', workspace)
            provider = AuditedProvider(RecordingTutor(), audit, 'teacher')
            async def emit(event):pass
            runtime = LiveRuntime(TutorSession('student-test', 3), provider, provider,
                                  'gemini', 'test-key', emit, lambda *args: None)
            runtime.provider = Provider()
            runtime.audit = audit
            await runtime.gemini_event({'toolCall': {'functionCalls': [
                {'id': 'close-1', 'name': 'close_board', 'args': {}}]}})
            await asyncio.gather(*runtime.tools.values())
            runtime.provider.ready = False
            self.assertFalse(await runtime.send_live_image({
                'id': 'unsent', 'image': 'data:image/jpeg;base64,/9j/'}))
            events = [json.loads(line) for line in audit.events.read_text(encoding='utf-8').splitlines()]
            self.assertTrue(any(item['kind'] == 'live_tool_call' and item['call']['id'] == 'close-1'
                                for item in events))
            self.assertTrue(any(item['kind'] == 'live_tool_result' and item['call_id'] == 'close-1'
                                for item in events))
            self.assertFalse(any(item['kind'] == 'live_image_sent' for item in events))
            await runtime.close()

    async def test_audit_write_failure_does_not_block_tutor_response(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / 'teacher'
            workspace.mkdir()
            audit = SessionAudit(root / 'audit', 'e' * 32, 'basic', 'codex', workspace)
            provider = AuditedProvider(RecordingTutor(), audit, 'teacher')
            with patch('server.audit.Path.open', side_effect=OSError('locked')):
                response = await provider.turn('請給我提示', schema={'required': []})
            self.assertIsNotNone(response)
            self.assertEqual(audit.sequence, 1)

    async def test_three_day_retention_removes_only_finished_expired_lessons(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / 'teacher'
            workspace.mkdir()
            old_id = 'a' * 32
            recent_id = 'b' * 32
            old = SessionAudit(root / 'audit', old_id, 'basic', 'codex', workspace)
            recent = SessionAudit(root / 'audit', recent_id, 'basic', 'codex', workspace)
            now = time.time()
            expired = now - RETENTION_SECONDS - 1
            os.utime(old.events, (expired, expired))
            self.assertEqual(prune_audits(root / 'audit', active_ids={old_id}, now=now), [])
            self.assertEqual(prune_audits(root / 'audit', now=now), [old_id])
            self.assertFalse(old.root.exists())
            self.assertTrue(recent.root.exists())

    async def test_interrupted_lesson_resumes_the_same_log(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / 'teacher'
            workspace.mkdir()
            sid = 'c' * 32
            first = SessionAudit(root / 'audit', sid, 'basic', 'codex', workspace)
            first.record('lesson_event', event_kind='student_utterance', payload={'text': '測試問題'})
            first.finish()
            resumed = SessionAudit.resume(root / 'audit', sid)
            self.assertIsNotNone(resumed)
            resumed.record('finalization_completed', summary=None)
            events = [json.loads(line) for line in resumed.events.read_text(encoding='utf-8').splitlines()]
            self.assertEqual([item['sequence'] for item in events], list(range(1, len(events) + 1)))
            self.assertIn('session_resumed', [item['kind'] for item in events])
            self.assertEqual(len(list((root / 'audit' / sid).glob('*.jsonl'))), 1)

    async def test_resume_discards_only_truncated_final_event(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / 'teacher'
            workspace.mkdir()
            sid = 'f' * 32
            audit = SessionAudit(root / 'audit', sid, 'basic', 'codex', workspace)
            with audit.events.open('ab') as stream:stream.write(b'{"incomplete":')
            resumed = SessionAudit.resume(root / 'audit', sid)
            self.assertIsNotNone(resumed)
            events = [json.loads(line) for line in audit.events.read_text(encoding='utf-8').splitlines()]
            self.assertEqual([item['kind'] for item in events], ['session_started', 'session_resumed'])

    async def test_only_expired_generated_agy_logs_are_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = root / ('agy-' + 'a' * 32 + '.log')
            current = root / ('agy-' + 'b' * 32 + '.log')
            unrelated = root / 'agy-family-notes.log'
            for path in (old, current, unrelated):path.write_text('test', encoding='utf-8')
            now = time.time()
            expired = now - RETENTION_SECONDS - 1
            for path in (old, unrelated):os.utime(path, (expired, expired))
            self.assertEqual(prune_provider_logs(root, now=now), 1)
            self.assertFalse(old.exists())
            self.assertTrue(current.exists())
            self.assertTrue(unrelated.exists())

    async def test_prune_skips_symlink_root_and_continues_after_locked_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            audit_root = root / 'audit'
            audit_root.mkdir()
            workspace = root / 'teacher'
            workspace.mkdir()
            ids = ['a' * 32, 'b' * 32]
            now = time.time()
            for sid in ids:
                audit = SessionAudit(audit_root, sid, 'basic', 'codex', workspace)
                os.utime(audit.events, (now - RETENTION_SECONDS - 1,) * 2)
            real_rmtree = __import__('shutil').rmtree
            def remove(path):
                if path.name == ids[0]:raise OSError('locked')
                real_rmtree(path)
            with patch('server.audit.shutil.rmtree', side_effect=remove):
                self.assertEqual(prune_audits(audit_root, now=now), [ids[1]])
            self.assertTrue((audit_root / ids[0]).exists())
            self.assertFalse((audit_root / ids[1]).exists())
            link = root / 'linked-audit'
            try:link.symlink_to(audit_root, target_is_directory=True)
            except (OSError, NotImplementedError):return
            self.assertEqual(prune_audits(link, now=now), [])
            self.assertEqual(prune_provider_logs(link, now=now), 0)
            with self.assertRaises(OSError):
                SessionAudit(link, 'c' * 32, 'basic', 'codex', workspace)
            self.assertTrue((audit_root / ids[0]).exists())

    async def test_connection_log_retention_keeps_recent_lines_in_old_file(self):
        import importlib
        module = importlib.import_module('server.app')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'connection.log'
            old = datetime.fromtimestamp(time.time() - RETENTION_SECONDS - 60, timezone.utc)
            recent = datetime.now(timezone.utc)
            path.write_text(f'{old:%Y-%m-%dT%H:%M:%S}Z INFO old\n'
                            f'{recent:%Y-%m-%dT%H:%M:%S}Z INFO recent\n', encoding='utf-8')
            with patch.object(module, 'ROOT', root):
                module.prune_connection_logs(include_current=True)
            self.assertEqual(path.read_text(encoding='utf-8'),
                             f'{recent:%Y-%m-%dT%H:%M:%S}Z INFO recent\n')


if __name__ == '__main__': unittest.main()
