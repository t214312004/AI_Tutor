from .test_support import install_test_seed
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from unittest.mock import patch
from .test_support import SeededStore as Store

class LifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_releases_active_runtime(self):
        from unittest.mock import AsyncMock, Mock
        from . import app
        closing=Mock(close=AsyncMock(return_value={}))
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app,'ROOT',Path(directory)),patch.object(app,'store',None), \
             patch.object(app,'bridge',closing),patch.object(app,'active',{'id':'synthetic'}), \
             patch.dict(os.environ,{'TUTOR_PARENT_WATCH':'0','TUTOR_TEST_MODE':'0'}):
            async with app.lifespan(app.app):pass
            closing.close.assert_awaited_once()
            self.assertIsNone(app.active)
            self.assertIsNone(app.bridge)

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.folder.name))
    def tearDown(self):
        # SQLite connections are short lived; collect closed context connections on Windows.
        import gc; gc.collect()
        self.folder.cleanup()
    def test_isolation_and_late_writes(self):
        sid=self.store.start('student-2','basic','test')
        self.store.event('student-2',sid,'student_note',{'text':'小二記錄'})
        self.assertEqual(len(self.store.history('student-4')['events']),0)
        self.store.finish('student-2',sid)
        with self.assertRaises(ValueError):self.store.event('student-2',sid,'student_note',{})
        with self.assertRaises(ValueError):self.store.student('../other')
    def test_restart_marks_abandoned_sessions(self):
        self.store.start('student-2','basic','')
        reopened=Store(Path(self.folder.name))
        self.assertEqual(reopened.history('student-2')['sessions'][0]['status'],'interrupted')

    def test_finalization_survives_restart_and_is_idempotent(self):
        sid=self.store.start('student-2','basic','')
        self.store.event('student-2',sid,'student_utterance',{'text':'這題怎麼算？'})
        self.store.begin_finalization('student-2',sid,False,'codex',{'captured':0})
        self.store.begin_finalization('student-2',sid,False,'codex',{'captured':0})
        reopened=Store(Path(self.folder.name))
        self.assertEqual(len(reopened.pending_finalizations()),1)
        snapshot=reopened.freeze_finalization('student-2',sid)
        self.assertEqual(len(reopened.finalization('student-2',sid)['source_hash']),64)
        self.assertEqual(sum(e['kind']=='capture_stats' for e in snapshot['events']),1)
        reopened.complete_finalization('student-2',sid,None)
        self.assertEqual(reopened.history('student-2')['sessions'][0]['status'],'ended')
    def test_finalization_accepts_only_post_lesson_usage_after_snapshot(self):
        sid=self.store.start('student-2','gpt','')
        self.store.begin_finalization('student-2',sid,True,'gpt_configured')
        self.store.freeze_finalization('student-2',sid)
        with self.assertRaises(ValueError):
            self.store.event('student-2',sid,'student_note',{'text':'too late'})
        self.store.event('student-2',sid,'post_lesson_api_usage',{'model':'gpt-6.1-sol'})
        self.assertEqual(self.store.history('student-2')['events'][-1]['kind'],'post_lesson_api_usage')
        self.store.complete_finalization('student-2',sid,None)
        with self.assertRaises(ValueError):
            self.store.event('student-2',sid,'post_lesson_api_usage',{})
    def test_finalization_rejects_a_stale_learning_evidence_snapshot(self):
        sid=self.store.start('student-2','basic','')
        self.store.record_learning_evidence('student-2',sid,{
            'code':'N-2-2','subject':'數學','label':'加法','kind':'asked_for_help',
            'quote':'這題不會','origin':'student_utterance','source_page':12})
        self.store.begin_finalization('student-2',sid,True,'codex')
        snapshot=self.store.freeze_finalization('student-2',sid)
        evidence_id=snapshot['evidence'][0]['id']
        self.store.correct_learning_evidence('student-2',evidence_id,exclude=True)
        with self.assertRaisesRegex(ValueError,'學習證據已更新'):
            self.store.complete_finalization('student-2',sid,{'analysis':{}})
        updated=self.store.freeze_finalization('student-2',sid,force=True)
        self.assertEqual(updated['evidence'],[])
    def test_parent_correction_invalidates_completed_ai_summary(self):
        sid=self.store.start('student-2','basic','')
        self.store.record_learning_evidence('student-2',sid,{
            'code':'N-2-2','subject':'數學','label':'加法','kind':'asked_for_help',
            'quote':'這題不會','origin':'student_utterance','source_page':12})
        self.store.begin_finalization('student-2',sid,True,'codex')
        snapshot=self.store.freeze_finalization('student-2',sid)
        self.store.complete_finalization('student-2',sid,{'analysis':{'difficulties':[]}})
        self.store.correct_learning_evidence('student-2',snapshot['evidence'][0]['id'],exclude=True)
        job=self.store.finalization('student-2',sid)
        self.assertEqual(job['state'],'failed')
        self.assertIsNone(job['summary'])
        self.store.retry_finalization('student-2',sid)
        self.assertEqual(self.store.freeze_finalization('student-2',sid)['evidence'],[])

    def test_legacy_students_receive_school_once(self):
        root=Path(self.folder.name)
        with self.store.registry() as db:
            db.execute('DROP TABLE students')
            db.execute('CREATE TABLE students (id TEXT PRIMARY KEY, name TEXT, grade INTEGER, preferences TEXT NOT NULL DEFAULT "")')
            db.executemany('INSERT INTO students VALUES (?,?,?,?)',[
                ('student-2','原有小二',2,''),('student-4','原有小四',4,''),
                ('student-other','其他學生',5,'')])
        migrated=Store(root)
        by_id={s['id']:s for s in migrated.students()}
        self.assertEqual(by_id['student-2']['school_id'],'demo-school')
        self.assertEqual(by_id['student-4']['school_id'],'demo-school')
        self.assertIsNone(by_id['student-other']['school_id'])
        migrated.save_profile('student-2','原有小二','',None,'')
        self.assertIsNone(next(s for s in Store(root).students() if s['id']=='student-2')['school_id'])

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        os.environ['TUTOR_DATA_DIR']=self.folder.name;install_test_seed(self.folder.name)
        os.environ['TUTOR_CORE_TOKEN']='test-token'
        from . import app
        self.module=importlib.reload(app)
        self.module.runtime_factory=None
        self.client=TestClient(self.module.app)
        self.client.__enter__()
        self.headers={'Authorization':'Bearer test-token'}
    def tearDown(self):
        self.client.__exit__(None,None,None)
        import gc;gc.collect();self.folder.cleanup()
    def test_auth_and_single_session(self):
        self.assertEqual(self.client.get('/bootstrap').status_code,401)
        body={'student_id':'student-2','mode':'basic','notes':'test'}
        r=self.client.post('/sessions',json=body,headers=self.headers)
        self.assertEqual(r.status_code,200);sid=r.json()['id']
        self.assertEqual(self.client.post('/sessions',json=body,headers=self.headers).status_code,409)
        self.client.post(f'/sessions/{sid}/end',json={},headers=self.headers)
        r=self.client.post(f'/sessions/{sid}/events',json={'kind':'student_note','payload':{}},headers=self.headers)
        self.assertEqual(r.status_code,409)

    def test_evidence_correction_cors_preflight(self):
        response=self.client.options('/students/student-2/learning/evidence/example',headers={
            'Origin':'http://127.0.0.1:5173',
            'Access-Control-Request-Method':'PATCH',
            'Access-Control-Request-Headers':'authorization,content-type'})
        self.assertEqual(response.status_code,200)
        self.assertIn('PATCH',response.headers['access-control-allow-methods'])

    def test_invalid_content_length_returns_client_error(self):
        for value in ('invalid','-1'):
            response=self.client.get('/bootstrap',headers={**self.headers,'Content-Length':value})
            self.assertEqual(response.status_code,400)

    def test_concurrent_starts_do_not_orphan_a_session(self):
        import asyncio
        from fastapi import HTTPException
        async def factory(state):
            await asyncio.sleep(.01)
            return None
        self.module.runtime_factory=factory
        async def concurrent():
            body=self.module.Start(student_id='student-2',mode='basic',teacher_route={'provider':'codex'})
            return await asyncio.gather(self.module.start(body),self.module.start(body),return_exceptions=True)
        results=self.client.portal.call(concurrent)
        self.assertEqual(sum(isinstance(result,dict) for result in results),1)
        failures=[result for result in results if isinstance(result,HTTPException)]
        self.assertEqual([result.status_code for result in failures],[409])
        sessions=self.module.store.history('student-2')['sessions']
        self.assertEqual(len(sessions),1)
        self.client.post('/sessions/'+sessions[0]['id']+'/end',headers=self.headers)

    def test_clear_history_removes_reviews_and_evidence_source_keys(self):
        sid=self.module.store.start('student-2','basic','')
        self.module.store.record_learning_evidence('student-2',sid,{
            'code':'N-2-2','subject':'數學','label':'加法','kind':'asked_for_help',
            'quote':'這題不會','origin':'student_utterance','source_page':12,'source_id':'test-source'})
        self.module.store.finish('student-2',sid)
        self.module.store.set_session_context('student-2',sid,False,'家長排除')
        response=self.client.delete('/students/student-2/history',headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        with self.module.store.student('student-2') as db:
            for table in ('sessions','session_reviews','learning_sources','learning_evidence'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
    def test_offline_finalization_returns_only_after_saved(self):
        import time
        body={'student_id':'student-2','mode':'basic','notes':'test'}
        started=self.client.post('/sessions',json=body,headers=self.headers).json()
        sid=started['id']
        self.assertFalse(started['online'])
        first=self.client.post(f'/sessions/{sid}/finalize',json={'captured':2},headers=self.headers)
        self.assertEqual(first.status_code,200)
        again=self.client.post(f'/sessions/{sid}/finalize',json={'captured':2},headers=self.headers)
        self.assertEqual(again.status_code,200)
        for _ in range(30):
            status=self.client.get(f'/sessions/{sid}/finalization',headers=self.headers).json()
            if status['state']=='completed':break
            time.sleep(.05)
        self.assertEqual(status['state'],'completed')
        self.assertFalse(status['summary_available'])
        history=self.client.get('/students/student-2/history',headers=self.headers).json()
        self.assertEqual(history['sessions'][0]['status'],'ended')
        self.assertEqual(sum(e['kind']=='capture_stats' for e in history['events']),1)
        second=self.client.post('/sessions',json=body,headers=self.headers)
        self.assertEqual(second.status_code,200)
        self.client.post(f"/sessions/{second.json()['id']}/end",headers=self.headers)
    def test_unknown_modes_rejected(self):
        r=self.client.post('/sessions',json={'student_id':'student-2','mode':'fake'},headers=self.headers)
        self.assertEqual(r.status_code,422)
        missing=self.client.post('/sessions',json={'student_id':'missing','mode':'basic'},headers=self.headers)
        self.assertEqual(missing.status_code,404)

    def test_only_subscription_modes_can_start_and_old_key_routes_are_gone(self):
        bootstrap = self.client.get('/bootstrap', headers=self.headers).json()
        self.assertEqual(set(bootstrap['capabilities']), {'codex', 'agy'})
        for mode in ('gpt', 'gemini'):
            response = self.client.post('/sessions', json={'student_id': 'student-2', 'mode': mode},
                                        headers=self.headers)
            self.assertEqual(response.status_code, 422)
        for route in ('/credentials', '/transcribe', '/diagnostics/tts'):
            self.assertEqual(self.client.post(route, json={}, headers=self.headers).status_code, 404)
        for engine in ('codex', 'agy'):
            response = self.client.post('/sessions', json={'student_id': 'student-2', 'basic_voice': engine},
                                        headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['backend'], engine)
            self.client.post(f"/sessions/{response.json()['id']}/end", headers=self.headers)


    def test_each_student_has_independent_school_context(self):
        original=self.client.get('/bootstrap',headers=self.headers).json()
        by_id={s['id']:s for s in original['students']}
        known_id=by_id['student-2']['school_id']
        self.assertEqual(by_id['student-4']['school_id'],known_id)
        self.assertIsNone(by_id['student-test']['school_id'])
        response=self.client.put('/students/student-4',headers=self.headers,json={
            'name':'小四同學','grade':4,'preferences':'','school_name':'另一所國小'})
        self.assertEqual(response.status_code,200)
        updated=self.client.get('/bootstrap',headers=self.headers).json()
        by_id={s['id']:s for s in updated['students']}
        other_id=by_id['student-4']['school_id']
        self.assertNotEqual(other_id,known_id)
        self.assertEqual(by_id['student-2']['school_id'],known_id)
        self.assertTrue(updated['schools'][known_id]['textbooks']['2'])
        self.assertEqual(updated['schools'][other_id]['school'],'另一所國小')
        self.assertEqual(updated['schools'][other_id]['textbooks'],{})
        self.assertEqual(updated['schools'][other_id]['events'],[])
        with self.module.store.registry() as db:
            db.execute('UPDATE schools SET context_json=? WHERE id=?',
                       ('{"textbooks":{"4":{"數學":"另一版本"}},"events":[{"date":"2026-12-01","name":"校慶"}]}',other_id))
        separate=self.client.get('/bootstrap',headers=self.headers).json()['schools']
        self.assertEqual(separate[other_id]['textbooks']['4']['數學'],'另一版本')
        self.assertEqual(separate[known_id]['textbooks']['4']['數學'],'合成練習題')
        self.assertNotIn({'date':'2026-12-01','name':'校慶'},separate[known_id]['events'])
        # Existing clients that omit school_name retain the student's choice.
        self.client.put('/students/student-4',headers=self.headers,json={
            'name':'小四同學','grade':4,'preferences':'備註'})
        self.assertEqual(next(s for s in self.client.get('/bootstrap',headers=self.headers).json()['students']
                              if s['id']=='student-4')['school_id'],other_id)
        self.client.put('/students/student-2',headers=self.headers,json={
            'name':'小二同學','grade':2,'preferences':'','school_name':'另一所國小'})
        self.assertEqual(next(s for s in self.client.get('/bootstrap',headers=self.headers).json()['students']
                              if s['id']=='student-2')['school_id'],other_id)
        self.client.put('/students/student-4',headers=self.headers,json={
            'name':'小四同學','grade':4,'preferences':'','school_name':''})
        self.assertIsNone(next(s for s in self.client.get('/bootstrap',headers=self.headers).json()['students']
                               if s['id']=='student-4')['school_id'])

    def test_clear_history_removes_only_selected_students_cli_notes(self):
        for student in ('student-2','student-4'):
            folder=self.module.ROOT/'runtime'/student/'lesson-1'/'teacher'
            folder.mkdir(parents=True)
            (folder/'scratch-note.txt').write_text('temporary lesson note',encoding='utf-8')
        response=self.client.delete('/students/student-2/history',headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertFalse((self.module.ROOT/'runtime'/'student-2').exists())
        self.assertTrue((self.module.ROOT/'runtime'/'student-4'/'lesson-1'/'teacher'/'scratch-note.txt').exists())

    def test_clear_history_keeps_records_when_workspace_removal_fails(self):
        sid=self.module.store.start('student-2','basic','test')
        self.module.store.finish('student-2',sid)
        workspace=self.module.ROOT/'runtime'/'student-2'
        workspace.mkdir(parents=True)
        (workspace/'note.txt').write_text('temporary',encoding='utf-8')
        with patch('server.app.shutil.rmtree',side_effect=OSError('locked')):
            response=self.client.delete('/students/student-2/history',headers=self.headers)
        self.assertEqual(response.status_code,409)
        self.assertTrue(any(row['id']==sid for row in self.module.store.history('student-2')['sessions']))

    def test_test_student_start_never_removes_old_records(self):
        sid=self.module.store.start('student-test','basic','old')
        self.module.store.finish('student-test',sid)
        workspace=self.module.ROOT/'runtime'/'student-test'
        workspace.mkdir(parents=True)
        (workspace/'note.txt').write_text('temporary',encoding='utf-8')
        with patch('server.app.shutil.rmtree',side_effect=OSError('locked')):
            response=self.client.post('/sessions',json={'student_id':'student-test','mode':'basic'},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertIsNotNone(self.module.active)
        self.assertTrue(any(row['id']==sid for row in self.module.store.history('student-test')['sessions']))
        self.assertTrue((workspace/'note.txt').exists())
        self.client.post('/sessions/'+response.json()['id']+'/end',headers=self.headers)







    def test_parent_can_review_correct_and_exclude_learning_evidence(self):
        sid=self.client.post('/sessions',json={'student_id':'student-2','mode':'basic'},headers=self.headers).json()['id']
        ident=self.module.store.record_learning_evidence('student-2',sid,{
            'code':'N-2-2','subject':'數學','label':'加減算式','kind':'asked_for_help',
            'quote':'這題不會','origin':'student_utterance','source_page':23})
        self.client.post(f'/sessions/{sid}/end',headers=self.headers)
        base='/students/student-2/learning'
        self.assertEqual(len(self.client.get('/students/student-2/history',headers=self.headers).json()['learning_profile']),1)
        self.assertEqual(self.client.put(base+'/N-2-2',json={
            'subject':'數學','label':'加減算式','status':'needs_review','note':'先畫圖'},headers=self.headers).status_code,200)
        self.assertEqual(self.client.patch(base+'/evidence/'+ident,json={'quote':'孩子問進位'},headers=self.headers).status_code,200)
        self.assertEqual(self.client.delete(base+'/evidence/'+ident,headers=self.headers).status_code,200)
        history=self.client.get('/students/student-2/history',headers=self.headers).json()
        self.assertEqual(history['learning_evidence'],[])
        self.assertEqual(history['learning_profile'][0]['help_count'],0)
        self.assertEqual(len(history['learning_edits']),2)
        self.assertEqual(self.client.get('/students/student-4/history',headers=self.headers).json()['learning_profile'],[])
        self.client.delete('/students/student-2/history',headers=self.headers)
        self.assertEqual(self.client.get('/students/student-2/history',headers=self.headers).json()['learning_profile'],[])

    def test_history_handles_local_finalization_with_null_analysis(self):
        from .post_lesson import memory_payload
        sid=self.module.store.start('student-2','basic','練習減法')
        self.module.store.event('student-2',sid,'student_utterance',{'text':'這題借位怎麼算？'})
        self.module.store.begin_finalization('student-2',sid,online=True,backend='codex')
        snapshot=self.module.store.freeze_finalization('student-2',sid)
        payload=memory_payload(snapshot)
        self.assertIsNone(payload['analysis'])
        self.module.store.complete_finalization('student-2',sid,payload)
        res=self.client.get('/students/student-2/history',headers=self.headers)
        self.assertEqual(res.status_code,200)
        history=res.json()
        self.assertEqual(history['finalizations'][0]['summary']['student_questions'],['這題借位怎麼算？'])
        self.assertIsNone(history['finalizations'][0]['summary']['analysis'])

    def test_student_test_can_resume_existing_session_without_audit_collision(self):
        first=self.client.post('/sessions',json={'student_id':'student-test','mode':'basic'},headers=self.headers)
        self.assertEqual(first.status_code,200)
        sid=first.json()['id']
        # Simulate an interrupted session where the audit folder and checkpoint already exist on disk.
        checkpoint=self.module.ROOT/'runtime'/'student-test'/'collaboration'/'lessons'/sid/'internal'/'checkpoint.json'
        checkpoint.parent.mkdir(parents=True,exist_ok=True)
        checkpoint.write_text('{}',encoding='utf-8')
        self.module.finish_audit(sid)
        self.module.store.finish('student-test',sid,'interrupted')
        self.module.active=None
        resumed=self.client.post('/sessions',json={'student_id':'student-test','mode':'basic','resume_session_id':sid},headers=self.headers)
        self.assertEqual(resumed.status_code,200)
        self.assertEqual(resumed.json()['id'],sid)
        self.client.post(f'/sessions/{sid}/end',headers=self.headers)

if __name__=='__main__':unittest.main()
