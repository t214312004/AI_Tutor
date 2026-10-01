from .test_support import install_test_seed
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from unittest.mock import patch
from .test_support import SeededStore as Store

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
    def test_online_finalization_commits_ai_summary(self):
        import time
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        async def runtime_factory(state):
            async def close():return {'usd':0,'status':'test'}
            return SimpleNamespace(session=SimpleNamespace(id=state['id']),close=close)
        self.module.runtime_factory=runtime_factory
        answer={key:[] for key in ('highlights','difficulties','guidance','next_steps','uncertainties')}
        with patch('server.post_lesson.analyze',new_callable=AsyncMock,return_value=(answer,'codex',1)) as analyze:
            started=self.client.post('/sessions',json={'student_id':'student-2','mode':'basic'},headers=self.headers).json()
            sid=started['id'];self.assertTrue(started['online'])
            self.module.store.event('student-2',sid,'student_utterance',{'text':'這題我需要提示'})
            self.client.post(f'/sessions/{sid}/finalize',json={},headers=self.headers)
            for _ in range(40):
                status=self.client.get(f'/sessions/{sid}/finalization',headers=self.headers).json()
                if status['state']=='completed':break
                time.sleep(.05)
            self.assertEqual(status['state'],'completed')
            self.assertTrue(status['summary_available'])
            self.assertEqual(analyze.await_count,1)
            history=self.client.get('/students/student-2/history',headers=self.headers).json()
            self.assertEqual(history['learning_context'][0]['analysis'],answer)
    def test_failed_analysis_can_be_retried_without_duplicate_session(self):
        import time
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        async def runtime_factory(state):
            async def close():return {'usd':0,'status':'test'}
            return SimpleNamespace(session=SimpleNamespace(id=state['id']),close=close)
        self.module.runtime_factory=runtime_factory
        answer={key:[] for key in ('highlights','difficulties','guidance','next_steps','uncertainties')}
        with patch('server.post_lesson.analyze',new_callable=AsyncMock,
                   side_effect=[RuntimeError('provider'),RuntimeError('provider'),(answer,'codex',1)]) as analyze:
            sid=self.client.post('/sessions',json={'student_id':'student-2','mode':'basic'},headers=self.headers).json()['id']
            self.module.store.event('student-2',sid,'student_utterance',{'text':'要提示'})
            self.client.post(f'/sessions/{sid}/finalize',json={},headers=self.headers)
            for _ in range(70):
                status=self.client.get(f'/sessions/{sid}/finalization',headers=self.headers).json()
                if status['state']=='failed':break
                time.sleep(.05)
            self.assertEqual(status['state'],'failed')
            self.assertTrue(status['snapshot_saved'])
            self.client.post(f'/sessions/{sid}/finalization/retry',json={},headers=self.headers)
            for _ in range(40):
                status=self.client.get(f'/sessions/{sid}/finalization',headers=self.headers).json()
                if status['state']=='completed':break
                time.sleep(.05)
            self.assertEqual(status['state'],'completed')
            self.assertEqual(analyze.await_count,3)
            self.assertEqual(len([s for s in self.module.store.history('student-2')['sessions'] if s['id']==sid]),1)
    def test_unknown_modes_rejected(self):
        r=self.client.post('/sessions',json={'student_id':'student-2','mode':'fake'},headers=self.headers)
        self.assertEqual(r.status_code,422)
        missing=self.client.post('/sessions',json={'student_id':'missing','mode':'basic'},headers=self.headers)
        self.assertEqual(missing.status_code,404)

    def test_gpt_delegation_switch_is_validated_per_new_session(self):
        from unittest.mock import AsyncMock
        from .gpt_config import api_models
        responses={'version':1,'delegation':{'type':'responses','provider':'openai',
                   'model':'gpt-6.1-sol','effort':'low'},
                   'vision':{'provider':'openai','model':'gpt-6-luna','effort':'max'}}
        client={**responses,'delegation':{'type':'client','provider':'codex',
                'model':'gpt-6.1-sol','effort':'medium'}}
        options={'openai':{'models':api_models(),'error':None},
                 'codex':{'models':[{'id':'gpt-6.1-sol','label':'Sol',
                                    'efforts':['low','medium'],'image':True}], 'error':None}}
        self.assertEqual(self.client.post('/sessions',json={
            'student_id':'student-2','mode':'gpt','gpt_live':responses},
            headers=self.headers).status_code,503)
        self.module.keys['OPENAI_API_KEY']='test-key'
        seen=[]
        async def runtime_factory(state):
            seen.append(state['gpt_live'])
            return None
        self.module.runtime_factory=runtime_factory
        with patch('server.gpt_config.route_options',new_callable=AsyncMock,return_value=options),\
             patch('server.gpt_config.verify_openai_models',new_callable=AsyncMock) as verify:
            for config in (responses,client):
                started=self.client.post('/sessions',json={
                    'student_id':'student-2','mode':'gpt','gpt_live':config},
                    headers=self.headers)
                self.assertEqual(started.status_code,200,started.text)
                self.assertEqual(started.json()['gpt_live']['delegation']['type'],
                                 config['delegation']['type'])
                self.assertEqual(self.client.post(
                    f"/sessions/{started.json()['id']}/end",headers=self.headers).status_code,200)
            self.assertEqual(verify.await_count,2)
        self.assertEqual([item['delegation']['type'] for item in seen],['responses','client'])
        self.assertEqual(seen[0]['vision']['model'],'gpt-6-luna')
        self.assertEqual(seen[1]['delegation']['effort'],'medium')

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

    def test_basic_route_enabled_by_key_and_cost_saved(self):
        from .test_basic import Teacher,Speech
        self.module.keys['GROQ_API_KEY']='test-only'
        with patch('server.providers.LazyCodex',side_effect=lambda *args:Teacher()),patch('server.basic.SpeechService',return_value=Speech()):
            response=self.client.post('/sessions',json={'student_id':'student-4','mode':'basic'},headers=self.headers)
            self.assertTrue(response.json()['online']);sid=response.json()['id']
            self.assertEqual(self.module.bridge.runtime.session.grade,4)
            with self.client.websocket_connect(f'/sessions/{sid}/stream') as ws:
                ws.send_json({'type':'authenticate','token':'test-token'})
                self.assertEqual(ws.receive_json()['type'],'ready')
                ws.send_json({'type':'audio','session_id':sid,'pcm':'AAA='})
                events=[]
                while not events or events[-1]['type']!='speech':
                    events.append(ws.receive_json())
                self.assertIn('audio_status',[event['type'] for event in events])
                ws.send_json({'type':'close','session_id':sid})
                self.assertEqual(ws.receive_json()['type'],'closed')
            self.client.post(f'/sessions/{sid}/end',json={},headers=self.headers)
        history=self.client.get('/students/student-4/history',headers=self.headers).json()
        self.assertTrue(any(e['kind']=='session_cost' for e in history['events']))
        self.assertEqual(self.client.get('/students/student-2/history',headers=self.headers).json()['events'],[])

    def test_basic_teacher_and_observer_use_selected_backend(self):
        from .test_basic import Teacher,Speech
        self.module.keys['GROQ_API_KEY']='test-only'
        with patch('server.providers.LazyCodex',side_effect=lambda *args:Teacher()) as codex, \
             patch('server.providers.AgyTutor',side_effect=lambda *args:Teacher()) as agy, \
             patch('server.basic.SpeechService',return_value=Speech()):
            workdirs=[]
            for backend,selected,other in [('agy',agy,codex),('codex',codex,agy)]:
                selected.reset_mock();other.reset_mock()
                response=self.client.post('/sessions',json={'student_id':'student-2','mode':'basic','backend':backend},headers=self.headers)
                self.assertTrue(response.json()['online'])
                self.assertEqual(selected.call_count,1)
                runtime=self.module.bridge.runtime
                self.assertIs(runtime.teacher,runtime.observer)
                workdir=selected.call_args.args[0]
                workdirs.append(workdir)
                self.assertTrue((workdir/'AGENTS.md').exists())
                other.assert_not_called()
                self.client.post('/sessions/'+response.json()['id']+'/end',headers=self.headers)
                self.assertTrue((workdir/'AGENTS.md').exists())
            self.assertEqual(*workdirs)

    def test_new_basic_session_recovers_workspace_and_previous_summary(self):
        from .test_basic import Teacher,Speech
        self.module.keys['GROQ_API_KEY']='test-only'
        with patch('server.providers.LazyCodex',side_effect=lambda *args:Teacher()), \
             patch('server.basic.SpeechService',return_value=Speech()):
            first=self.client.post('/sessions',json={'student_id':'student-2','mode':'basic'},headers=self.headers).json()
            original=self.module.bridge.runtime
            workdir=original.photo_workspace.root
            (workdir/'teacher-notes.txt').write_text('下次先看位值',encoding='utf-8')
            original.session.heard_history.append({'role':'user','text':'進位還不熟'})
            self.client.post('/sessions/'+first['id']+'/end',headers=self.headers)
            second=self.client.post('/sessions',json={'student_id':'student-2','mode':'basic'},headers=self.headers).json()
            restored=self.module.bridge.runtime
            self.assertNotEqual(first['id'],second['id'])
            self.assertEqual(restored.photo_workspace.root,workdir)
            self.assertEqual((workdir/'teacher-notes.txt').read_text(encoding='utf-8'),'下次先看位值')
            self.assertEqual(restored.session.memory[0]['student_questions'],['進位還不熟'])
            self.assertEqual(restored.session.workspace_context['photo_archive'],'internal/photos/')
            self.client.post('/sessions/'+second['id']+'/end',headers=self.headers)

    def test_basic_rejects_unavailable_selected_cli_before_stream(self):
        from .providers import ProviderError
        self.module.keys['GROQ_API_KEY']='test-only'
        class Unavailable:
            def __init__(self,*args):pass
            async def ensure_ready(self):raise ProviderError('Codex 尚未登入此 Windows 帳號')
        with patch('server.providers.LazyCodex',Unavailable):
            response=self.client.post('/sessions',json={'student_id':'student-2','mode':'basic'},headers=self.headers)
        self.assertEqual(response.status_code,503)
        self.assertIn('Codex 尚未登入',response.json()['detail'])
        self.assertIsNone(self.module.active)
        self.assertIsNone(self.module.bridge)

    def test_live_runtime_is_provisioned_without_policy_gate(self):
        for mode,key in [('gpt','OPENAI_API_KEY'),('gemini','GEMINI_API_KEY')]:
            self.module.keys[key]='test-only'
            response=self.client.post('/sessions',json={'student_id':'student-2','mode':mode},headers=self.headers)
            self.assertTrue(response.json()['online'])
            # No socket connection means no provider API is called by this test.
            self.assertEqual(self.module.bridge.runtime.mode,mode)
            self.client.post('/sessions/'+response.json()['id']+'/end',json={},headers=self.headers)

    def test_live_ignores_basic_backend_selection(self):
        from .test_basic import Teacher
        self.module.keys['OPENAI_API_KEY']='test-only'
        self.module.keys['GEMINI_API_KEY']='test-only'
        with patch('server.providers.FailoverTutor',side_effect=lambda *args:Teacher()) as failover, \
             patch('server.providers.AgyTutor',side_effect=AssertionError('basic agy selected')), \
             patch('server.providers.LazyCodex',side_effect=AssertionError('basic codex selected')):
            for mode,backend,expected in (
                ('gpt','codex','agy_codex_failover'),
                ('gemini','agy','gemini_native_vision_agy_codex_tutor')):
                failover.reset_mock()
                response=self.client.post('/sessions',json={
                    'student_id':'student-2','mode':mode,'backend':backend},headers=self.headers)
                self.assertTrue(response.json()['online'])
                self.assertEqual(response.json()['backend'],expected)
                self.assertEqual(failover.call_count,2)
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

if __name__=='__main__':unittest.main()
