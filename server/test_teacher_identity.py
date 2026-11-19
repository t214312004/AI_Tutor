from .test_support import install_test_seed
import importlib
import os
import tempfile
import unittest
from fastapi.testclient import TestClient
from .identity import DEFAULT_TEACHER_NAME, student_profile


class TeacherIdentityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        os.environ['TUTOR_DATA_DIR'] = self.folder.name;install_test_seed(self.folder.name)
        os.environ['TUTOR_CORE_TOKEN'] = 'test-token'
        from . import app
        self.module = importlib.reload(app)
        self.module.runtime_factory = None
        self.client = TestClient(self.module.app)
        self.client.__enter__()
        self.headers = {'Authorization': 'Bearer test-token'}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        import gc; gc.collect()
        self.folder.cleanup()

    def test_student_defaults(self):
        s2 = student_profile('student-2', self.folder.name)
        self.assertEqual(s2['teacher_name'], '範例老師甲')

        s4 = student_profile('student-4', self.folder.name)
        self.assertEqual(s4['teacher_name'], '伴讀老師')

    def test_unknown_student_fallback(self):
        unknown = student_profile('unknown-student')
        self.assertEqual(unknown['teacher_name'], DEFAULT_TEACHER_NAME)

    def test_bootstrap_endpoint_includes_teacher_defaults_and_voices(self):
        res = self.client.get('/bootstrap', headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn('teacher_defaults', data)
        self.assertIn('codex_voice_options', data)
        self.assertEqual(data['teacher_defaults']['student-2']['teacher_name'], '範例老師甲')
        self.assertEqual(data['teacher_defaults']['student-4']['teacher_name'], '伴讀老師')

    def test_start_session_custom_teacher(self):
        self.module.runtime_factory = None
        res = self.client.post('/sessions', json={
            'student_id': 'student-2',
            'mode': 'basic',
            'teacher_name': '自訂老師甲',
            'edge_voice': 'zh-TW-HsiaoYuNeural',
        }, headers=self.headers)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body['teacher_name'], '自訂老師甲')
        self.assertNotIn('edge_voice', body)

    def test_start_session_default_teacher_fallback(self):
        self.module.runtime_factory = None
        res = self.client.post('/sessions', json={
            'student_id': 'student-2',
            'mode': 'basic',
        }, headers=self.headers)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body['teacher_name'], '範例老師甲')
        self.assertNotIn('edge_voice', body)

    def test_start_session_ignores_retired_edge_voice_field(self):
        self.module.runtime_factory = None
        res = self.client.post('/sessions', json={
            'student_id': 'student-2',
            'mode': 'basic',
            'edge_voice': 'zh-TW-YunJhenNeural',
        }, headers=self.headers)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertNotIn('edge_voice', body)


if __name__ == '__main__':
    unittest.main()
