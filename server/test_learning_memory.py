import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .test_support import SeededStore as Store
from .tutor import TutorEngine, TutorSession

EXAMPLE = {'code':'N-2-2','subject':'數學','label':'加減算式與直式計算',
           'kind':'asked_for_help','quote':'我不會這題加減法',
           'origin':'student_utterance','source_page':23}

class LearningMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.root=Path(self.folder.name)
        self.store=Store(self.root)
    def tearDown(self):
        import gc;gc.collect();self.folder.cleanup()

    async def test_long_term_is_per_student_and_completion_is_not_mastery(self):
        sid=self.store.start('student-2','basic','數學')
        self.store.event('student-2',sid,'student_utterance',{'text':EXAMPLE['quote']})
        self.store.event('student-2',sid,'learning_summary',{
            'student_questions':[EXAMPLE['quote']], 'tasks':[{'status':'done','title':'數學習作'}]})
        evidence_id=self.store.record_learning_evidence('student-2',sid,EXAMPLE)
        self.store.finish('student-2',sid)
        self.assertEqual(Store(self.root).learning_profile('student-2')[0]['help_count'],1)
        self.assertEqual(self.store.learning_profile('student-2')[0]['parent_status'],'unassessed')
        self.assertEqual(self.store.learning_profile('student-4'),[])
        self.store.correct_learning_evidence('student-2',evidence_id,'孩子說加法不知道如何進位')
        self.assertEqual(self.store.learning_evidence('student-2')[0]['quote'],'孩子說加法不知道如何進位')
        self.store.correct_learning_evidence('student-2',evidence_id,exclude=True)
        self.assertEqual(self.store.learning_evidence('student-2'),[])
        self.assertEqual(self.store.learning_profile('student-2'),[])
        self.assertNotIn(EXAMPLE['quote'],self.store.learning_context('student-2')[0]['student_questions'])
        self.store.set_learning_override('student-2','N-2-2','數學','加減算式','needs_review','先用位值表')
        profile=Store(self.root).learning_profile('student-2')
        self.assertEqual((profile[0]['parent_status'],profile[0]['parent_note']),('needs_review','先用位值表'))
        self.assertEqual(profile[0]['help_count'],0)

    async def test_parent_guidance_reaches_teacher_as_data(self):
        self.store.set_learning_override('student-2','N-2-2','數學','加減算式','needs_review','先畫位值表')
        session=TutorSession('student-2',2,'數學')
        session.long_term_memory=self.store.learning_profile('student-2')
        session.memory=[{'session_id':'earlier','student_questions':['加法怎麼算？']}]
        ref={'code':'N-2-2','subject':'數學','kind':'學習內容','grade_min':2,'grade_max':2,
             'summary':'加減算式與直式計算','source_title':'數學課綱','page':23,
             'source_url':'https://example.test/math','quality':'auto_math_row'}
        class Recorder:
            prompt=''
            async def turn(self,text,**kwargs):
                self.prompt=text
                return json.dumps({'quiet':False,'speech':'先看個位。','board_html':'',
                    'close_board':False,'need_capture':False,'evidence':[]})
        recorder=Recorder()
        with patch('server.tutor.teaching_references',return_value=[ref]):
            await TutorEngine(recorder).respond(session,'加減法怎麼做？')
        self.assertIn('先畫位值表',recorder.prompt)
        self.assertIn('求助或觀察次數不等於能力評分',recorder.prompt)
        self.assertIn('needs_review',recorder.prompt)
        self.assertIn('"previous_sessions"',recorder.prompt)
        self.assertIn('"session_id": "earlier"',recorder.prompt)

    async def test_excluded_session_is_removed_from_summary_and_learning_profile(self):
        sid=self.store.start('student-2','basic','')
        self.store.event('student-2',sid,'learning_summary',
                         {'student_questions':[EXAMPLE['quote']],'tasks':[]})
        self.store.record_learning_evidence('student-2',sid,EXAMPLE)
        self.store.finish('student-2',sid)
        self.assertEqual(self.store.learning_profile('student-2')[0]['help_count'],1)
        self.store.set_session_context('student-2',sid,False,'轉錄待複核')
        self.assertEqual(self.store.learning_context('student-2'),[])
        self.assertEqual(self.store.learning_profile('student-2'),[])
        self.assertEqual(len(self.store.learning_evidence('student-2')),1)
        self.store.set_session_context('student-2',sid,True)
        self.assertEqual(self.store.learning_profile('student-2')[0]['help_count'],1)

    async def test_evidence_edit_changes_only_its_own_session_summary(self):
        question='這題加法怎麼算？'
        first=self.store.start('student-2','basic','')
        self.store.event('student-2',first,'learning_summary',{'student_questions':[question]})
        evidence=self.store.record_learning_evidence('student-2',first,
            {**EXAMPLE,'quote':question})
        self.store.finish('student-2',first)
        second=self.store.start('student-2','basic','')
        self.store.event('student-2',second,'learning_summary',{'student_questions':[question]})
        self.store.finish('student-2',second)
        self.store.correct_learning_evidence('student-2',evidence,'孩子問第一題加法')
        context={item['session_id']:item for item in self.store.learning_context('student-2')}
        self.assertEqual(context[first]['student_questions'],['孩子問第一題加法'])
        self.assertEqual(context[second]['student_questions'],[question])
        self.store.correct_learning_evidence('student-2',evidence,exclude=True)
        context={item['session_id']:item for item in self.store.learning_context('student-2')}
        self.assertEqual(context[first]['student_questions'],[])
        self.assertEqual(context[second]['student_questions'],[question])

    async def test_existing_session_database_migrates_without_losing_history(self):
        folder=self.root/'student-2'
        old=folder/'learning.sqlite3'
        old.unlink()
        db=sqlite3.connect(old)
        db.executescript('''CREATE TABLE sessions(id TEXT PRIMARY KEY,started TEXT,ended TEXT,status TEXT,mode TEXT,notes TEXT,grade INTEGER);
            CREATE TABLE events(id TEXT PRIMARY KEY,session_id TEXT,kind TEXT,payload TEXT,created TEXT);
            INSERT INTO sessions VALUES('old-session','2026-09-01','2026-09-01','ended','basic','舊紀錄',2);
            PRAGMA user_version=1;''')
        db.commit();db.close()
        reopened=Store(self.root)
        self.assertEqual(reopened.history('student-2')['sessions'][0]['id'],'old-session')
        self.assertEqual(reopened.learning_profile('student-2'),[])
        with reopened.student('student-2') as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],2)

if __name__=='__main__':unittest.main()
