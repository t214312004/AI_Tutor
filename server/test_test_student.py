from .test_support import install_test_seed
import importlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from .test_support import SeededStore as Store
from .identity import student_profile


class TestStudentTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        os.environ['TUTOR_DATA_DIR'] = self.folder.name;install_test_seed(self.folder.name)
        os.environ['TUTOR_CORE_TOKEN'] = 'test-token'
        from . import app
        self.module = importlib.reload(app)
        self.client = TestClient(self.module.app)
        self.client.__enter__()
        self.headers = {'Authorization': 'Bearer test-token'}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        import gc; gc.collect()
        self.folder.cleanup()

    def test_student_test_seeded_and_ordered_last(self):
        store = Store(Path(self.folder.name))
        students = store.students()
        student_ids = [s['id'] for s in students]
        self.assertIn('student-test', student_ids)
        self.assertEqual(student_ids[-1], 'student-test')
        test_student = next(s for s in students if s['id'] == 'student-test')
        self.assertEqual(test_student['name'], '測試同學')
        self.assertEqual(test_student['grade'], 3)
        self.assertTrue(test_student['preferences'])

    def test_existing_database_adds_student_test_without_overwriting(self):
        root = Path(self.folder.name)
        with Store(root).registry() as db:
            db.execute("UPDATE students SET preferences='小二特別偏好' WHERE id='student-2'")
            db.execute("DELETE FROM students WHERE id='student-test'")
        reopened = Store(root)
        students = reopened.students()
        s2 = next(s for s in students if s['id'] == 'student-2')
        self.assertEqual(s2['preferences'], '小二特別偏好')
        st = next(s for s in students if s['id'] == 'student-test')
        self.assertEqual(st['name'], '測試同學')

    def test_identity_defaults_for_student_test(self):
        profile = student_profile('student-test')
        self.assertEqual(profile['teacher_name'], '伴讀老師')
        self.assertEqual(profile['edge_voice'], 'zh-TW-HsiaoYuNeural')

    def test_test_student_keeps_session_summary_without_concept_assessment(self):
        store = Store(Path(self.folder.name))
        sid = store.start('student-test', 'basic', '測試開始')
        # Emitting events and learning evidence
        store.event('student-test', sid, 'student_utterance', {'text': '請問這題怎麼解？'})
        item = {
            'code': 'N-2-2',
            'subject': '數學',
            'label': '直式加減',
            'kind': 'asked_for_help',
            'quote': '請問這題怎麼解？',
            'origin': 'student_utterance',
            'source_page': 12,
        }
        res = store.record_learning_evidence('student-test', sid, item)
        self.assertEqual(res, 'test-evidence')
        self.assertEqual(store.learning_evidence('student-test'), [])
        self.assertEqual(store.learning_profile('student-test'), [])
        self.assertEqual(store.learning_context('student-test'), [])
        store.finish('student-test',sid)
        self.assertEqual(store.learning_context('student-test')[0]['student_questions'],['請問這題怎麼解？'])

    def test_preferences_and_grade_configurable_via_api(self):
        res = self.client.put(
            '/students/student-test',
            json={
                'name': '測試專用生',
                'preferences': '請多用白板圖解，引導反問。',
                'grade': 4,
            },
            headers=self.headers,
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['saved'])

        # Verify bootstrap returns updated preferences and grade
        boot = self.client.get('/bootstrap', headers=self.headers).json()
        st = next(s for s in boot['students'] if s['id'] == 'student-test')
        self.assertEqual(st['name'], '測試專用生')
        self.assertEqual(st['preferences'], '請多用白板圖解，引導反問。')
        self.assertEqual(st['grade'], 4)

    def test_start_session_preserves_workspace_and_preferences(self):
        self.module.runtime_factory = None
        # First set preferences
        self.client.put(
            '/students/student-test',
            json={
                'name': '測試同學',
                'preferences': '家長偏好：請以問句引導。',
                'grade': 2,
            },
            headers=self.headers,
        )

        # Create dummy old workspace and dummy session
        old_runtime = self.module.ROOT / 'runtime' / 'student-test' / 'old-session'
        old_runtime.mkdir(parents=True)
        (old_runtime / 'test.txt').write_text('old runtime note', encoding='utf-8')

        # Start session for student-test
        res = self.client.post(
            '/sessions',
            json={'student_id': 'student-test', 'mode': 'basic', 'notes': '今日測試數學習作'},
            headers=self.headers,
        )
        self.assertEqual(res.status_code, 200)
        sid = res.json()['id']
        offline_log = self.module.ROOT / 'runtime' / 'student-test' / 'audit' / sid / 'lesson.jsonl'
        self.assertTrue(offline_log.exists())

        # A new test session must not erase files left by an earlier one.
        self.assertTrue((self.module.ROOT / 'runtime' / 'student-test' / 'old-session' / 'test.txt').exists())

        # End session
        self.client.post(f'/sessions/{sid}/end', json={}, headers=self.headers)
        self.assertIn('session_closed', offline_log.read_text(encoding='utf-8'))

        # Ensure preferences are still preserved after clearing/finishing
        boot = self.client.get('/bootstrap', headers=self.headers).json()
        st = next(s for s in boot['students'] if s['id'] == 'student-test')
        self.assertEqual(st['preferences'], '家長偏好：請以問句引導。')
        self.assertEqual(st['grade'], 2)

    def test_online_test_student_creates_session_review_folder(self):
        from .test_basic import Speech
        from .test_basic_workspace import RecordingTutor
        self.module.keys['GROQ_API_KEY'] = 'test-only'
        with patch('server.providers.LazyCodex', side_effect=lambda *args: RecordingTutor()), \
             patch('server.basic.SpeechService', return_value=Speech()):
            response = self.client.post('/sessions', json={
                'student_id': 'student-test', 'mode': 'basic'}, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            sid = response.json()['id']
            review = self.module.ROOT / 'runtime' / 'student-test' / 'audit' / sid
            self.assertTrue((review / 'lesson.jsonl').exists())
            self.assertTrue((review / 'AGENTS.md').exists())
            self.module.diagnostics.info('synthetic_transport_step')
            event = self.client.post(f'/sessions/{sid}/events', json={
                'kind': 'client_diagnostic', 'payload': {'event': 'camera_error', 'detail': 'synthetic'}},
                headers=self.headers)
            self.assertEqual(event.status_code, 200)
            ended = self.client.post(f'/sessions/{sid}/end', json={}, headers=self.headers)
            self.assertEqual(ended.status_code, 200)
            self.assertTrue((review / 'workspace-after' / 'AGENTS.md').exists())
            log = [json.loads(line) for line in (review / 'lesson.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertTrue(any(item['kind'] == 'core_diagnostic' and
                                item['message'] == 'synthetic_transport_step' for item in log))
            self.assertTrue(any(item['kind'] == 'lesson_event' and
                                item['event_kind'] == 'client_diagnostic' for item in log))
            self.assertTrue(any(item['kind'] == 'session_closed' for item in log))
            cleared = self.client.delete('/students/student-test/history', headers=self.headers)
            self.assertEqual(cleared.status_code, 200)
            self.assertFalse(review.exists())

    def test_finalization_snapshots_post_lesson_workspace(self):
        self.module.runtime_factory = None
        started = self.client.post('/sessions', json={
            'student_id': 'student-test', 'mode': 'basic'}, headers=self.headers)
        self.assertEqual(started.status_code, 200)
        sid = started.json()['id']
        workspace = self.module.ROOT / 'runtime' / 'student-test' / 'post-lesson' / sid
        workspace.mkdir(parents=True)
        (workspace / 'notes.txt').write_text('課後分析工作資料', encoding='utf-8')
        result = self.client.post(f'/sessions/{sid}/finalize', json={}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        for _ in range(40):
            status = self.client.get(f'/sessions/{sid}/finalization', headers=self.headers).json()
            if status['state'] == 'completed':break
            time.sleep(.05)
        self.assertEqual(status['state'], 'completed')
        review = self.module.ROOT / 'runtime' / 'student-test' / 'audit' / sid
        for _ in range(40):
            if (review / 'post-lesson-workspace' / 'notes.txt').exists():break
            time.sleep(.05)
        self.assertEqual((review / 'post-lesson-workspace' / 'notes.txt').read_text(encoding='utf-8'),
                         '課後分析工作資料')

    def test_audit_write_failure_does_not_strand_finalization(self):
        self.module.runtime_factory = None
        started = self.client.post('/sessions', json={
            'student_id': 'student-test', 'mode': 'basic'}, headers=self.headers)
        self.assertEqual(started.status_code, 200)
        sid = started.json()['id']
        audit = self.module.audits[sid]
        audit.events = audit.root / 'unavailable' / 'lesson.jsonl'
        result = self.client.post(f'/sessions/{sid}/finalize', json={}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        for _ in range(40):
            status = self.client.get(f'/sessions/{sid}/finalization', headers=self.headers).json()
            if status['state'] == 'completed':break
            time.sleep(.05)
        self.assertEqual(status['state'], 'completed')
        self.assertNotIn(sid, self.module.audits)

    def test_clear_history_refuses_linked_runtime_root(self):
        runtime = self.module.ROOT / 'runtime'
        if runtime.exists():self.skipTest('runtime root already exists')
        with tempfile.TemporaryDirectory() as external:
            marker = Path(external) / 'student-test' / 'keep.txt'
            marker.parent.mkdir()
            marker.write_text('keep', encoding='utf-8')
            try:runtime.symlink_to(external, target_is_directory=True)
            except (OSError, NotImplementedError):
                original = Path.is_symlink
                with patch.object(Path, 'is_symlink', lambda path: path == runtime or original(path)):
                    response = self.client.delete('/students/student-test/history', headers=self.headers)
            else:response = self.client.delete('/students/student-test/history', headers=self.headers)
            self.assertEqual(response.status_code, 409)
            self.assertTrue(marker.exists())


if __name__ == '__main__':
    unittest.main()
