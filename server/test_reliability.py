"""Synthetic regressions for races observed in a long multi-role lesson."""
import asyncio
import json
import os
import hashlib
import sqlite3
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import closing
from unittest.mock import patch
from types import SimpleNamespace

from .basic import BasicRuntime
from .collaboration import CollaborationWorkspace,CollaborativeProvider
from .collaboration_cli import atomic_text
from .curriculum_map import detect_subject
from .diagnostics import failure_details
from .test_basic import Teacher,Speech
from .test_collaboration import NaturalProvider
from .transcript_projection import TranscriptProjection,projected_records
from .tutor import TutorSession
from .work_lifecycle import LessonWork,applicable_observation


def decision(**extra):
    return {'quiet':False,'speech':'請接續。','board_html':'','close_board':False,
            'need_capture':False,'evidence':[],**extra}


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.events=[];self.saved=[]
    async def asyncTearDown(self):self.temp.cleanup()
    async def emit(self,event):self.events.append(event)





    async def test_basic_synthesis_cannot_play_after_wait_control(self):
        waiting=asyncio.Event();release=asyncio.Event()
        class DelayedSpeech(Speech):
            async def synthesize(inner,text):waiting.set();await release.wait();return {'audio':'','text':text,'boundaries':[]}
        runtime=BasicRuntime(TutorSession('synthetic',2),Teacher(),Teacher(),DelayedSpeech(),self.emit,
                             lambda kind,payload:self.saved.append((kind,payload)))
        task=asyncio.create_task(runtime.teach('這題怎麼算？',0))
        await waiting.wait();await runtime.interaction_control('listening');release.set();await task
        self.assertFalse(any(e['type']=='speech' for e in self.events))
        await runtime.close()



    async def test_checkpoint_debounce_flush_and_consecutive_history_dedupe(self):
        session=TutorSession('synthetic',2);workspace=CollaborationWorkspace(self.root,session,'basic')
        for _ in range(100):workspace.checkpoint()
        self.assertFalse((workspace.root/'internal/checkpoint.json').exists())
        for content in ('A','A','B','A'):
            workspace.publish('current-lesson.md',content,workspace.valid())
        self.assertEqual(len(list((workspace.shared/'history').glob('*.md'))),3)
        session.committed_sequence=42;await workspace.close()
        saved=json.loads((workspace.root/'internal/checkpoint.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['committed_sequence'],42)
        workspace._write_checkpoint(1,'{}')
        self.assertEqual(json.loads((workspace.root/'internal/checkpoint.json').read_text(encoding='utf-8'))['committed_sequence'],42)

    async def test_failed_multi_file_publication_restores_latest_prior_consensus(self):
        session=TutorSession('synthetic',2);workspace=CollaborationWorkspace(self.root,session,'basic')
        workspace.publish('current-lesson.md','原有方向',workspace.valid())
        work=LessonWork(session,'新問題')
        with work.bind():
            workspace.publish('current-lesson.md','第一份草稿',workspace.valid())
            workspace.publish('current-lesson.md','最後草稿',workspace.valid())
            def fail():raise PermissionError(13,'synthetic write failure')
            work.stage_file('notes',self.root/'notes.md',fail,lock=workspace._publication_lock)
        # A newer independent observation can arrive during the request. Undo
        # restores the version present at commit, not a stale staging backup.
        workspace.publish('current-lesson.md','提交前的方向',workspace.valid())
        with work.bind():
            with self.assertRaises(PermissionError):work.commit()
        self.assertFalse(work.committed);self.assertEqual(session.committed_sequence,0)
        self.assertEqual((workspace.shared/'current-lesson.md').read_text(encoding='utf-8'),'提交前的方向')
        await workspace.close()

    async def test_selected_historical_photo_is_only_applicable_to_bound_work(self):
        session=TutorSession('synthetic',2,camera_enabled=True)
        photo={'received':time.monotonic(),'historical':True,'evidence':['歷史題目']}
        session.observation=photo
        self.assertIsNone(applicable_observation(session))
        work=LessonWork(session,'核對剛才的題目');work.observation=photo
        with work.bind():self.assertIs(applicable_observation(session),photo)
        session.camera_enabled=False
        with work.bind():self.assertIsNone(applicable_observation(session))

    async def test_completed_photo_keeps_eligibility_while_teacher_finishes_decision(self):
        session=TutorSession('synthetic',2,camera_enabled=True)
        workspace=CollaborationWorkspace(self.root,session,'basic')
        clock=[time.monotonic()];timer=SimpleNamespace(monotonic=lambda:clock[0])
        with patch('server.collaboration.time',timer),patch('server.work_lifecycle.time',timer):
            frame={'id':'pinned','received':clock[0],'captured_at':'synthetic'}
            work=LessonWork(session,'核對這張材料')
            with work.bind():
                clock[0]+=69
                self.assertTrue(workspace.record_observation(frame,'指定歷史照片的內容',workspace.valid(),allow_historical=True))
                clock[0]+=45
                self.assertTrue(work.commit())
        self.assertIn('指定歷史照片',(workspace.shared/'latest-observation.md').read_text(encoding='utf-8'))
        await workspace.close()

    async def test_slow_vision_survives_routine_camera_update_without_native_reset(self):
        session=TutorSession('synthetic',2,basic_workflow=True)
        workspace=CollaborationWorkspace(self.root,session,'basic');session.collaboration=workspace
        clock=[time.monotonic()]
        class SlowVision(NaturalProvider):
            async def turn(inner,*args,**kwargs):clock[0]+=69;return '這張照片有六乘六的格子。'
        vision=SlowVision()
        runtime=BasicRuntime(session,CollaborativeProvider(NaturalProvider(),workspace,'teacher'),
            CollaborativeProvider(vision,workspace,'vision'),Speech(),self.emit,lambda *args:None,workspace.photos)
        runtime.photo_workspace=None
        timer=SimpleNamespace(monotonic=lambda:clock[0])
        with patch('server.classroom_runtime.time',timer),patch('server.collaboration.time',timer),patch('server.work_lifecycle.time',timer):
            frame={'id':'old','image':'data:image/jpeg;base64,/9j/','received':clock[0],'camera_epoch':0,'captured_at':'synthetic'}
            runtime.frame={**frame,'id':'new'}
            text=await runtime.observe_natural(frame)
            self.assertIn('六乘六',text);self.assertEqual(vision.invalidations,0)
            self.assertTrue(session.observation['historical'])
            self.assertIsNone(applicable_observation(session))
        await runtime.close()

    async def test_utf8_read_answer_reuses_completed_original_request(self):
        root=self.root/'teacher';(root/'internal').mkdir(parents=True)
        shutil.copyfile(Path(__file__).with_name('collaboration_cli.py'),root/'internal/bridge.py')
        turn=root/'drafts/abc';request=turn/'requests/one';request.mkdir(parents=True)
        atomic_text(root/'internal/active-turn.json',json.dumps({'token':'abc','role':'teacher'}))
        atomic_text(turn/'question.md','核對「鬱」字與🙂')
        atomic_text(request/'question.md','核對「鬱」字與🙂');atomic_text(request/'answer.md','已看到「鬱」字與🙂')
        proc=await asyncio.create_subprocess_exec(sys.executable,'-X','utf8',str(root/'internal/bridge.py'),
            '--turn','abc','read-answer','--file','question.md',stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,env={**os.environ,'PYTHONIOENCODING':'utf-8'})
        out,err=await proc.communicate()
        self.assertEqual(proc.returncode,0,err.decode('utf-8'))
        self.assertIn('「鬱」字與🙂',out.decode('utf-8'))
        self.assertEqual(len(list((turn/'requests').iterdir())),1)



    async def test_reviewed_repair_preserves_quote_backup_and_is_idempotent(self):
        from scripts.repair_learning_evidence import repair
        from .test_support import SeededStore
        from .test_learning_memory import EXAMPLE
        (self.root/'data').mkdir()
        store=SeededStore(self.root/'data');sid=store.start('student-2','basic','合成課堂')
        ident=store.record_learning_evidence('student-2',sid,EXAMPLE)
        store.finish('student-2',sid)
        manifest={'student_id':'student-2','records':[{'id':ident,'quote_sha256':hashlib.sha256(EXAMPLE['quote'].encode()).hexdigest()}]}
        preview=repair(store.root,manifest,self.root/'backup')
        self.assertFalse(preview['applied']);self.assertEqual(preview['pending'],1)
        with patch('scripts.repair_learning_evidence.atomic_text',side_effect=OSError('backup write failed')):
            with self.assertRaises(OSError):repair(store.root,manifest,self.root/'backup',True)
        self.assertEqual(repair(store.root,manifest,self.root/'backup')['pending'],1)
        result=repair(store.root,manifest,self.root/'backup',True)
        self.assertEqual(result['excluded'],1)
        with store.student('student-2') as db:
            row=db.execute('SELECT valid,quote FROM learning_evidence WHERE id=?',(ident,)).fetchone()
            self.assertEqual((row['valid'],row['quote']),(0,EXAMPLE['quote']))
        with closing(sqlite3.connect(Path(result['backup'])/'learning.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT valid FROM learning_evidence WHERE id=?',(ident,)).fetchone()[0],1)
        self.assertEqual(repair(store.root,manifest,self.root/'backup',True)['excluded'],0)


class ProjectionTests(unittest.TestCase):
    def test_control_schema_is_accepted_by_strict_native_json_boundary(self):
        from .tutor import collaboration_schema,DECISION_SCHEMA
        from copy import deepcopy
        schema=collaboration_schema(deepcopy(DECISION_SCHEMA))
        self.assertEqual(set(schema['properties']),set(schema['required']))
    def test_interleaved_roles_preserve_spaces_repetitions_and_pause_boundaries(self):
        p=TranscriptProjection()
        student=p.append('user','I ',0,200,received=1)
        p.append('assistant','嗯',100,300,received=1.1)
        self.assertIs(p.append('user','I read.',200,600,received=1.2),student)
        self.assertEqual(student['text'],'I I read.')
        self.assertNotEqual(p.append('user','新問題',5000,5300,received=5)['id'],student['id'])

    def test_legacy_role_switches_do_not_cut_student_sentence(self):
        events=[{'id':str(i),'created':f'2026-10-02T00:00:0{i}Z','kind':'live_transcript',
                 'payload':{'role':role,'text':text,'transcript_id':str(i)}}
                for i,(role,text) in enumerate((('user','我先'),('assistant','好'),('user','念完。')))]
        rows=projected_records(events)
        user=next(r for r in rows if r['data']['role']=='user')
        self.assertEqual(user['data']['text'],'我先念完。');self.assertEqual(user['data']['sources'],['0','2'])
        self.assertFalse(user['data']['playback_verified'])

    def test_iso_date_is_not_math_but_real_subtraction_remains_math(self):
        self.assertNotEqual(detect_subject('2026-10-02T20:06:00 閱讀課文'), '數學')
        self.assertEqual(detect_subject('20-6 怎麼算？'),'數學')

    def test_windows_retry_only_proven_sharing_violation(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'共識.md';error=PermissionError(13,'sharing');error.winerror=32
            with patch('server.collaboration_cli.os.replace',side_effect=[error,None]) as replace,patch('server.collaboration_cli.time.sleep') as sleep:
                atomic_text(target,'內容');self.assertEqual(replace.call_count,2);sleep.assert_called_once()
            error.winerror=5
            with patch('server.collaboration_cli.os.replace',side_effect=error) as replace,patch('server.collaboration_cli.time.sleep') as sleep:
                with self.assertRaises(PermissionError):atomic_text(target,'內容')
                self.assertEqual(replace.call_count,1);sleep.assert_not_called()

    def test_diagnostics_does_not_include_prompt_or_account_path(self):
        error=PermissionError(13,'SECRET PROMPT');error.winerror=5
        data=failure_details('save',error,Path('private/account/key.json'))
        self.assertEqual(data['file'],'key.json');self.assertEqual(data['winerror'],5)
        self.assertNotIn('SECRET',str(data));self.assertNotIn('account',str(data))


if __name__=='__main__':unittest.main()
