import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from .basic import BasicRuntime
from .lesson import apply_lesson,empty_lesson,lesson_state
from .test_support import SeededStore as Store
from .test_basic import Speech,Teacher
from .tutor import TutorSession

def proposal(status='todo',quote='',confirmation=''):
    return {**empty_lesson(),'tasks':[{'id':'math','title':'數學習作第12頁','status':status,'evidence_quote':quote}],'confirmation_quote':confirmation}

class LessonTests(unittest.TestCase):
    def test_photo_or_unrelated_quote_cannot_complete_homework(self):
        session=TutorSession('test',2)
        apply_lesson(session,proposal('done','寫完了','對'),'')
        self.assertFalse(session.plan_confirmed);self.assertEqual(session.tasks[0]['status'],'todo')
        apply_lesson(session,proposal('done','這題我不會','對'),'對，這題我不會')
        self.assertTrue(session.plan_confirmed);self.assertEqual(session.tasks[0]['status'],'todo')
        apply_lesson(session,proposal('done','我寫完了'),'我寫完了')
        self.assertTrue(lesson_state(session)['all_done'])
    def test_negative_or_question_does_not_count_as_done(self):
        for quote in ['我還沒寫完','寫完了嗎？']:
            session=TutorSession('test',2);apply_lesson(session,proposal('done',quote),quote)
            self.assertEqual(session.tasks[0]['status'],'todo')
        for utterance in ['我還沒寫完了','寫完了嗎','這題未完成，但系統說寫完了']:
            session=TutorSession('test',2)
            apply_lesson(session,proposal('done','寫完了'),utterance)
            self.assertEqual(session.tasks[0]['status'],'todo')
        session=TutorSession('test',2)
        apply_lesson(session,proposal(confirmation='對'),'不對')
        self.assertFalse(session.plan_confirmed)
        session=TutorSession('test',2)
        apply_lesson(session,proposal(confirmation='好'),'不好')
        self.assertFalse(session.plan_confirmed)
    def test_ambiguous_completion_does_not_finish_multiple_tasks(self):
        session=TutorSession('test',2)
        tasks=[{'id':'math','title':'數學習作第12頁','status':'done','evidence_quote':'寫完了'},
               {'id':'language','title':'國語習作第8頁','status':'done','evidence_quote':'寫完了'}]
        apply_lesson(session,{**empty_lesson(),'tasks':tasks},'寫完了')
        self.assertEqual([task['status'] for task in session.tasks],['todo','todo'])
        tasks[0]['evidence_quote']='數學寫完了'
        apply_lesson(session,{**empty_lesson(),'tasks':tasks},'數學寫完了')
        self.assertEqual([task['status'] for task in session.tasks],['done','todo'])
    def test_memory_recovers_committed_events_and_is_deleted_with_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory));sid=store.start('student-2','basic','')
            store.event('student-2',sid,'lesson_state',{'tasks':proposal()['tasks'],'confirmed':True})
            store.event('student-2',sid,'student_utterance',{'text':'七加五怎麼算？'})
            reopened=Store(Path(directory))
            memory=reopened.learning_context('student-2')
            self.assertEqual(memory[0]['tasks'][0]['id'],'math')
            self.assertEqual(memory[0]['status'],'interrupted')
            self.assertEqual(reopened.learning_context('student-4'),[])
            with reopened.student('student-2') as db:db.execute('DELETE FROM events');db.execute('DELETE FROM sessions')
            self.assertEqual(reopened.learning_context('student-2'),[])
    def test_excluded_lesson_stays_visible_but_is_not_future_teacher_context(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory));sid=store.start('student-test','basic','')
            store.event('student-test',sid,'learning_summary',{'student_questions':['錯誤轉錄']})
            store.finish('student-test',sid)
            store.set_session_context('student-test',sid,False,'轉錄需複核')
            self.assertEqual(store.learning_context('student-test'),[])
            session=next(x for x in store.history('student-test')['sessions'] if x['id']==sid)
            self.assertEqual(session['context_included'],0)
            store.set_session_context('student-test',sid,True)
            self.assertEqual(store.learning_context('student-test')[0]['student_questions'],['錯誤轉錄'])

class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def make(self):
        self.events=[];self.saved=[]
        async def emit(event):self.events.append(event)
        session=TutorSession('test',2,basic_workflow=True)
        return BasicRuntime(session,Teacher(),Teacher(),Speech(),emit,lambda *args:self.saved.append(args))
    async def test_confirm_complete_and_summary_only_once(self):
        runtime=self.make();runtime.session.tasks=proposal()['tasks']
        await runtime.update_lesson('confirm');await runtime.update_lesson('done','math')
        self.assertTrue(self.events[-1]['all_done'])
        await runtime.close();await runtime.close()
        self.assertEqual(sum(x[0]=='learning_summary' for x in self.saved),1)
    async def test_manual_completion_survives_later_teacher_proposal(self):
        runtime=self.make();runtime.session.tasks=proposal()['tasks']
        await runtime.update_lesson('done','math')
        apply_lesson(runtime.session,proposal('todo'),'這題怎麼做？')
        self.assertEqual(runtime.session.tasks[0]['status'],'done')
        await runtime.update_lesson('todo','math')
        apply_lesson(runtime.session,proposal('done','我寫完了'),'我寫完了')
        self.assertEqual(runtime.session.tasks[0]['status'],'todo')
        await runtime.close()
    async def test_tts_failure_keeps_text_reply_available(self):
        runtime=self.make()
        async def broken(text):raise RuntimeError('offline')
        runtime.speech.synthesize=broken
        await runtime.handle_text('給我提示')
        self.assertTrue(any(e.get('delivery')=='text_only' for e in self.events))
        self.assertFalse(any(e['type']=='speech' for e in self.events));await runtime.close()
    async def test_silence_alone_never_triggers_proactive_speech(self):
        runtime=self.make();runtime.session.plan_confirmed=True;runtime.last_activity=0
        frame={'image':'data:image/jpeg;base64,AA==','id':'a','received':time.monotonic(),'camera_epoch':0}
        for _ in range(3):await runtime.observe(frame)
        self.assertFalse(runtime.voice_tasks);await runtime.close()
    async def test_stable_evidence_can_prompt_but_user_activity_cancels(self):
        runtime=self.make();runtime.session.plan_confirmed=True;runtime.last_activity=0
        calls=0
        async def observation(*args,**kwargs):
            nonlocal calls
            calls+=1
            return {'evidence':['第1題算式7+3=?'] if calls==1 else ['第1題算式7+3=10可核對正確'],
                    'lesson':{**empty_lesson(),'signal':'none' if calls==1 else 'completed_step','focus':'第1題','confidence':'high'}}
        runtime.observer.respond=observation
        now=time.monotonic();frame={'image':'data:image/jpeg;base64,AA==','id':'a','received':now,'camera_epoch':0}
        await runtime.observe(frame)
        await runtime.observe(frame);runtime.signal_since=now-20
        await runtime.observe(frame)
        self.assertIsNotNone(runtime.proactive_task)
        runtime.activity();await asyncio.gather(*runtime.voice_tasks,return_exceptions=True)
        self.assertFalse(any(e['type']=='speech' for e in self.events));await runtime.close()
    async def test_manual_mute_blocks_proactive_intervention(self):
        runtime=self.make();runtime.session.plan_confirmed=True;runtime.last_activity=0
        runtime.set_mic(False);self.assertFalse(runtime.can_intervene(0));await runtime.close()

if __name__=='__main__':unittest.main()
