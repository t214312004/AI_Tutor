"""Learning claims require sourced responses and explicit parent review."""
from datetime import datetime, timedelta, timezone
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from .test_support import SeededStore, install_test_seed
from .tutor import TutorSession
from .native_basic import NativeBasicRuntime
from .agy_runtime import AgyLiveRuntime
from .work_lifecycle import LessonWork

EXAMPLE = {'subject':'數學','concept':'進位加法','question':'36＋18是多少？',
           'response':'54','assistance':'none','phase':'immediate_check',
           'previous_attempt_id':'','answer_basis':'用位值表核對，未提供答案或提示。'}


class PracticeStorageTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.store=SeededStore(Path(self.folder.name))
        self.sid=self.store.start('student-2','basic','加法')

    def review(self, ident, **changes):
        item=self.store.learning_attempts('student-2')[0]
        self.store.review_learning_attempt('student-2',ident,{**EXAMPLE,**changes},'correct','人工核對',item['revision'])

    def test_opt_in_and_parent_review_do_not_become_mastery(self):
        self.assertFalse(self.store.practice_settings('student-2')['enabled'])
        with self.assertRaises(ValueError):self.store.record_learning_attempt('student-2',self.sid,EXAMPLE,'typed_text','u1')
        self.store.save_practice_settings('student-2',True,['carry-add'])
        ident=self.store.record_learning_attempt('student-2',self.sid,EXAMPLE,'typed_text','u1')
        self.assertEqual(self.store.learning_attempts('student-2',context_only=True),[])
        self.assertEqual(self.store.learning_profile('student-2'),[])
        self.review(ident)
        self.assertTrue(self.store.learning_attempts('student-2',context_only=True)[0]['independent_verified'])
        self.assertEqual(self.store.learning_profile('student-2'),[])
        self.assertEqual(self.store.learning_attempts('student-4'),[])

    def test_review_requires_answer_basis_preserves_history_and_rejects_stale_edits(self):
        ident=self.store.record_learning_attempt('student-2',self.sid,EXAMPLE)
        with self.assertRaises(ValueError):self.review(ident,answer_basis='')
        with self.assertRaises(ValueError):self.review(ident,response='')
        self.review(ident,assistance='hint')
        self.assertFalse(self.store.learning_attempts('student-2')[0]['independent_verified'])
        with self.assertRaises(ValueError):self.store.review_learning_attempt('student-2',ident,EXAMPLE,'correct','',1)
        self.store.review_learning_attempt('student-2',ident,EXAMPLE,'unreviewed','排除',2,exclude=True)
        self.assertEqual(self.store.learning_attempts('student-2'),[])
        with self.store.student('student-2') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM learning_attempt_edits').fetchone()[0],2)

    def test_delayed_check_requires_same_student_concept_and_elapsed_day(self):
        prior=self.store.record_learning_attempt('student-2',self.sid,EXAMPLE)
        delayed={**EXAMPLE,'phase':'delayed_check','previous_attempt_id':prior}
        with self.assertRaises(ValueError):self.store.record_learning_attempt('student-2',self.sid,delayed)
        next_sid=self.store.start('student-2','basic','')
        with self.assertRaises(ValueError):self.store.record_learning_attempt('student-2',next_sid,delayed)
        with self.store.student('student-2') as db:
            db.execute('UPDATE learning_attempts SET created=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(days=4)).isoformat(),prior))
        with self.assertRaises(ValueError):self.store.record_learning_attempt('student-2',next_sid,{**delayed,'concept':'面積'})
        other=self.store.start('student-4','basic','')
        with self.assertRaises(ValueError):self.store.record_learning_attempt('student-4',other,delayed)
        ident=self.store.record_learning_attempt('student-2',next_sid,delayed)
        item=next(x for x in self.store.learning_attempts('student-2') if x['id']==ident)
        self.assertGreaterEqual(item['elapsed_days'],4)
        with self.assertRaises(ValueError):self.store.review_learning_attempt('student-2',ident,EXAMPLE,'correct','',1)

    def test_whitespace_previous_attempt_id_rejected_for_delayed_check(self):
        with self.assertRaises(ValueError):
            self.store.record_learning_attempt('student-2', self.sid, {
                **EXAMPLE,
                'phase': 'delayed_check',
                'previous_attempt_id': '   ',
            })

    def test_source_deduplication_and_session_exclusion(self):
        self.store.save_practice_settings('student-2',True,[])
        ident=self.store.record_learning_attempt('student-2',self.sid,EXAMPLE,'provider_transcript','t1')
        duplicate=self.store.record_learning_attempt('student-2',self.sid,{**EXAMPLE,'response':'999'},'provider_transcript','t1')
        self.assertEqual(ident,duplicate)
        self.assertEqual(self.store.learning_attempts('student-2')[0]['response'],'54')
        self.review(ident)
        self.store.finish('student-2',self.sid)
        self.store.set_session_context('student-2',self.sid,False,'待查')
        self.assertEqual(self.store.learning_attempts('student-2',context_only=True),[])
        self.assertEqual(len(self.store.learning_attempts('student-2')),1)
        with self.assertRaises(ValueError):self.store.record_learning_attempt('student-2',self.sid,EXAMPLE,'typed_text','late')

    def test_missing_response_is_not_wrong_and_test_student_does_not_accumulate(self):
        value={**EXAMPLE,'response':'','answer_basis':''}
        ident=self.store.record_learning_attempt('student-2',self.sid,value)
        self.store.review_learning_attempt('student-2',ident,value,'not_observed','孩子中斷，沒有作答',1)
        entry=self.store.learning_attempts('student-2',context_only=True)[0]
        self.assertEqual(entry['result'],'not_observed');self.assertFalse(entry['independent_verified'])
        sid=self.store.start('student-test','basic','')
        self.assertIsNone(self.store.record_learning_attempt('student-test',sid,EXAMPLE))
        self.assertEqual(self.store.learning_attempts('student-test'),[])

    def test_settings_persist_and_existing_records_survive_schema_upgrade(self):
        self.store.save_practice_settings('student-2',True,['carry-add','carry-add'])
        reopened=SeededStore(Path(self.folder.name))
        self.assertEqual(reopened.practice_settings('student-2'),{'enabled':True,'reviewed_cards':['carry-add']})
        self.assertEqual(reopened.history('student-2')['sessions'][0]['id'],self.sid)
        with self.assertRaises(ValueError):reopened.save_practice_settings('student-2',True,['invented-card'])


class PracticeApiTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.env=patch.dict(os.environ,{'TUTOR_DATA_DIR':self.folder.name,'TUTOR_CORE_TOKEN':'practice-test','TUTOR_TEST_MODE':'1'})
        self.env.start();self.addCleanup(self.env.stop);install_test_seed(self.folder.name)
        from . import app
        self.module=importlib.reload(app)
        self.client=TestClient(self.module.app);self.client.__enter__();self.addCleanup(lambda:self.client.__exit__(None,None,None))
        self.headers={'Authorization':'Bearer practice-test'}
        self.sid=self.module.store.start('student-2','basic','')

    def test_parent_workflow_and_active_context_refresh(self):
        session=TutorSession('student-2',2)
        runtime=type('Runtime',(),{'session':session,'sync_context':AsyncMock()})()
        self.module.active={'student_id':'student-2','id':self.sid}
        self.module.bridge=type('Bridge',(),{'runtime':runtime})()
        # Avoid lifespan trying to close the minimal test adapter.
        self.addCleanup(lambda:setattr(self.module,'bridge',None))
        self.addCleanup(lambda:setattr(self.module,'active',None))
        root='/students/student-2/practice'
        self.assertEqual(self.client.put(root+'/settings',headers=self.headers,json={'enabled':True,'reviewed_cards':['carry-add']}).status_code,200)
        self.assertTrue(session.practice_checks_enabled);runtime.sync_context.assert_awaited_once()
        created=self.client.post(root+'/attempts',headers=self.headers,json={**EXAMPLE,'session_id':self.sid})
        self.assertEqual(created.status_code,200)
        ident=created.json()['id']
        self.assertEqual(session.learning_attempts,[])
        reviewed=self.client.put(root+'/attempts/'+ident,headers=self.headers,json={**EXAMPLE,'result':'correct','revision':1})
        self.assertEqual(reviewed.status_code,200)
        self.assertEqual(session.learning_attempts[0]['id'],ident)
        excluded=self.client.put(root+'/attempts/'+ident,headers=self.headers,json={**EXAMPLE,'result':'unreviewed','revision':2,'exclude':True})
        self.assertEqual(excluded.status_code,200);self.assertEqual(session.learning_attempts,[])

    def test_unknown_student_and_unverified_result_are_rejected(self):
        self.assertEqual(self.client.get('/students/missing/practice',headers=self.headers).status_code,404)
        body={**EXAMPLE,'session_id':self.sid,'result':'correct'}
        self.assertEqual(self.client.post('/students/student-2/practice/attempts',headers=self.headers,json=body).status_code,422)
        self.assertEqual(self.client.post('/students/student-4/practice/attempts',headers=self.headers,json={**EXAMPLE,'session_id':self.sid}).status_code,422)

    def test_export_and_clear_include_attempts_and_revisions_but_keep_settings(self):
        store=self.module.store
        store.save_practice_settings('student-2',True,['carry-add'])
        ident=store.record_learning_attempt('student-2',self.sid,EXAMPLE)
        store.review_learning_attempt('student-2',ident,EXAMPLE,'correct','人工核對',1)
        exported=self.client.get('/students/student-2/history',headers=self.headers).json()
        self.assertEqual(exported['practice_attempts'][0]['id'],ident)
        self.assertEqual(len(exported['practice_edits']),1)
        store.finish('student-2',self.sid)
        cleared=self.client.delete('/students/student-2/history',headers=self.headers)
        self.assertEqual(cleared.status_code,200)
        self.assertEqual(store.learning_attempts('student-2'),[])
        self.assertEqual(store.practice_edits('student-2'),[])
        self.assertTrue(store.practice_settings('student-2')['enabled'])


class NativePracticeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.observer=type('Observer',(),{'workspace':Path(self.folder.name),'vision_only':True,'turn':AsyncMock(),'close':AsyncMock()})()
        self.session=TutorSession('synthetic',2,'進位加法',input_speaker='student')
        self.session.memory=[{'student_questions':['前次摘要標記']}]
        self.session.long_term_memory=[{'parent_note':'家長註記標記'}]
        self.records=[]
        self.refs=patch('server.live_classroom.teaching_references',return_value=[]);self.refs.start();self.addCleanup(self.refs.stop)

    async def test_both_adapters_share_memory_rules_and_card_review(self):
        from .test_native_basic import FakeClient
        native=NativeBasicRuntime(self.session,self.observer,AsyncMock(),lambda *a:None,client=FakeClient())
        agy=AgyLiveRuntime(self.session,self.observer,AsyncMock(),lambda *a:None)
        self.addAsyncCleanup(native.close);self.addAsyncCleanup(agy.close)
        for prompt in (native.instructions(),agy.instructions()):
            self.assertIn('前次摘要標記',prompt);self.assertIn('家長註記標記',prompt)
            self.assertIn('不無限重複提問',prompt);self.assertIn('不為完成檢查延後結束',prompt)
        self.assertEqual(native.context()['teaching_cards'],[])
        self.session.reviewed_teaching_cards=['carry-add']
        self.assertEqual(native.context()['teaching_cards'][0]['review'],'parent_confirmed_for_this_student')
        self.session.reviewed_teaching_cards=['carry-add','english-recall']
        native.latest_user='英文單字如何遮住回想？'
        self.assertEqual([card['id'] for card in native.context()['teaching_cards']],['english-recall'])
        native.latest_user='英文文法的時態'
        self.assertEqual(native.context()['teaching_cards'],[])

    async def test_attempt_requires_opt_in_student_exact_quote_and_fresh_work(self):
        from .test_native_basic import FakeClient
        runtime=NativeBasicRuntime(self.session,self.observer,AsyncMock(),lambda k,p:self.records.append((k,p)),client=FakeClient())
        self.addAsyncCleanup(runtime.close)
        work=LessonWork(self.session,'我算出54',source='typed_text',source_id='typed1')
        args={**EXAMPLE,'context_version':runtime.context(work)['context_version']}
        async def call(call_id):return await runtime.dispatch_tool(('primary','typed1',call_id),work,'record_learning_attempt',args)
        self.assertFalse((await call('disabled'))[0])
        self.session.practice_checks_enabled=True
        args['response']='答案999';self.assertFalse((await call('fabricated'))[0])
        args['response']='54';self.assertTrue((await call('accepted'))[0])
        self.assertEqual(self.records[-2][0],'learning_attempt')
        self.assertNotIn('result',self.records[-2][1])
        self.session.plan_revision+=1
        self.assertFalse((await call('stale'))[0])

    async def test_agy_only_accepts_referenced_typed_input_and_confirmation(self):
        peer=type('Peer',(),{'conversation':'agy-test','write':AsyncMock(),'close':AsyncMock()})()
        runtime=AgyLiveRuntime(self.session,self.observer,AsyncMock(),lambda k,p:self.records.append((k,p)))
        runtime.connection=peer;runtime.connected=True
        self.session.practice_checks_enabled=True
        await runtime.send_text(peer,'我算出54')
        ref=runtime.current_verified_input()['ref']
        proposal={'tool':'record_learning_attempt','source_ref':ref,**EXAMPLE}
        self.assertIn('等待使用者',await runtime.handle_proposal(peer,proposal,None))
        self.assertFalse(any(k=='learning_attempt' for k,p in self.records))
        await runtime.confirm_tool(runtime.pending_tool[0],True)
        self.assertTrue(any(k=='learning_attempt' for k,p in self.records))
        await runtime.sync_context()
        self.assertIn('缺少可核對',await runtime.handle_proposal(peer,proposal,None))
        runtime.connection=None;runtime.connected=False

    async def test_memory_edits_refresh_native_and_agy_without_starting_a_new_turn(self):
        from .test_native_basic import FakeClient
        client=FakeClient()
        native=NativeBasicRuntime(self.session,self.observer,AsyncMock(),lambda *a:None,client=client)
        self.addAsyncCleanup(native.close)
        native.realtime=True
        self.session.long_term_memory=[{'parent_note':'更正後只用實物'}]
        await native.sync_context()
        self.assertEqual([name for name,payload in client.calls],['thread/inject_items'])
        text=client.calls[-1][1]['items'][0]['content'][0]['text']
        self.assertIn('更正後只用實物',text);self.assertNotIn('家長註記標記',text)
        proposal=Path(self.folder.name)/'proposal.json';proposal.write_text('{}')
        peer=type('Peer',(),{'conversation':'agy-test','write':AsyncMock(),'proposal':proposal,'close':AsyncMock()})()
        agy=AgyLiveRuntime(self.session,self.observer,AsyncMock(),lambda *a:None)
        agy.connection=peer;agy.connected=True;self.addAsyncCleanup(agy.close)
        notice=await agy.handle_hook(peer,0)
        self.assertIn('更正後只用實物',notice);peer.write.assert_not_called()
        await agy.sync_context()
        peer.close.assert_awaited_once();self.assertIsNone(agy.connection)

    async def test_hook_context_stays_below_transport_limit_and_sources_are_retained(self):
        self.session.notes='長'*8000
        self.session.native_history=[{'role':'user','text':'句'*8000}]*30
        self.session.memory=[{'student_questions':['問'*8000]*5,'heard_guidance':['答'*8000]*5}]*3
        self.session.long_term_memory=[{'parent_note':'註'*500,'recent_evidence':[{'quote':'證'*500}]*2}]*12
        self.session.learning_attempts=[{**EXAMPLE,'question':'題'*1000,'response':'答'*1000,'answer_basis':'據'*1000}]*8
        agy=AgyLiveRuntime(self.session,self.observer,AsyncMock(),lambda *a:None)
        self.addAsyncCleanup(agy.close)
        encoded=json.dumps(json.dumps(agy.reference_data(),ensure_ascii=False),ensure_ascii=False).encode()
        self.assertLess(len(encoded),240000)
