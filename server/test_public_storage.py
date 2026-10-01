"""Public installs and legacy storage must have separate, explicit behavior."""
import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from .storage import Store
from .identity import student_profile
from . import paths


class PublicStorageTests(unittest.TestCase):
    def test_blank_install_has_no_family_or_demo_students(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            store = Store(Path(directory))
            self.assertEqual(store.students(), [])
            self.assertEqual(store.schools(), [])
            self.assertEqual(student_profile('student-2')['teacher_name'], '伴讀老師')

    def test_seed_does_not_overwrite_existing_profile_or_school(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            seed = {'schools': [{'id': 'demo', 'school': '範例學校'}],
                    'students': [{'id': 'student-legacy', 'name': '範例同學', 'grade': 3, 'school_id': 'demo'}]}
            (root / 'bootstrap.json').write_text(json.dumps(seed), encoding='utf-8')
            store = Store(root)
            store.save_profile('student-legacy', '家長修改', '偏好已修改', 4, '')
            with store.registry() as db:
                db.execute("UPDATE schools SET context_json=? WHERE id='demo'", ('{"notice":"edited"}',))
            reopened = Store(root)
            profile = reopened.students()[0]
            self.assertEqual(profile['name'], '家長修改')
            self.assertEqual(profile['grade'], 4)
            self.assertIsNone(profile['school_id'])
            self.assertEqual(json.loads(reopened.schools()[0]['context_json'])['notice'], 'edited')

    def test_seed_rejects_path_traversal_before_creating_registry(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            (root / 'bootstrap.json').write_text(json.dumps({'students': [
                {'id': '../outside', 'name': '合成', 'grade': 3}]}), encoding='utf-8')
            with self.assertRaises(ValueError):
                Store(root)
            self.assertFalse((root / 'registry.sqlite3').exists())

    def test_explicit_test_data_skips_local_private_settings(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'TUTOR_DATA_DIR': directory}, clear=True):
            with patch.object(paths, 'PROJECT', Path(directory) / 'missing-project'):
                self.assertEqual(paths.data_dir(), Path(directory))
                self.assertEqual(paths.local_settings(), {})

    def test_invalid_private_location_never_falls_back(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            project = Path(directory)
            (project / 'app-data').mkdir()
            (project / 'app-data/local-settings.json').write_text(json.dumps({'home': str(project / 'missing')}), encoding='utf-8')
            with patch.object(paths, 'PROJECT', project), self.assertRaises(ValueError):
                paths.tutor_home()

    def test_home_override_skips_local_private_settings(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'TUTOR_HOME': directory}, clear=True):
            with patch.object(paths, 'local_settings', side_effect=AssertionError('private config read')):
                self.assertEqual(paths.tutor_home(), Path(directory))


class FirstRunApiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {'TUTOR_DATA_DIR': self.folder.name, 'TUTOR_CORE_TOKEN': 'first-run-test'}, clear=True)
        self.environment.start()
        from . import app
        self.module = importlib.reload(app)
        self.client = TestClient(self.module.app)
        self.client.__enter__()
        self.headers = {'Authorization': 'Bearer first-run-test'}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.environment.stop()
        self.folder.cleanup()

    def test_add_student_and_restart_keep_profile_and_record(self):
        self.assertEqual(self.client.get('/bootstrap', headers=self.headers).json()['students'], [])
        invalid = self.client.post('/students', headers=self.headers, json={'name': ' ', 'grade': 3})
        self.assertEqual(invalid.status_code, 422)
        created = self.client.post('/students', headers=self.headers, json={'name': '合成學生', 'grade': 5})
        self.assertEqual(created.status_code, 200)
        student = created.json()
        self.assertIsNone(student['school_id'])
        sid = self.module.store.start(student['id'], 'basic', '合成課堂')
        self.module.store.finish(student['id'], sid)
        reopened = Store(Path(self.folder.name))
        self.assertEqual(reopened.students()[0]['id'], student['id'])
        self.assertEqual(len(reopened.history(student['id'])['sessions']), 1)
        self.assertEqual(self.client.post('/demo', headers=self.headers, json={}).status_code, 409)

    def test_demo_is_explicit_synthetic_and_cannot_overwrite(self):
        self.assertEqual(self.client.post('/demo', headers=self.headers, json={}).status_code, 200)
        boot = self.client.get('/bootstrap', headers=self.headers).json()
        self.assertEqual(len(boot['students']), 3)
        self.assertIn('虛構', boot['schools']['demo-school']['school'])
        self.assertEqual(self.client.post('/demo', headers=self.headers, json={}).status_code, 409)
