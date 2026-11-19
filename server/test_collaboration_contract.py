"""Regression contracts for free prose, slow roles and one teacher's outputs."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from .basic import BasicRuntime
from .collaboration import CollaborationWorkspace, CollaborativeProvider, TEAM_GUIDELINES
from .collaboration_cli import atomic_text, ask
from .test_basic import Speech
from .test_collaboration import NaturalProvider
from .tutor import TutorSession, validate_student_output, validate_visible_text
from .work_lifecycle import LessonWork


class CollaborationContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.events=[];self.saved=[];self.runtimes=[];self.workspaces=[]

    async def asyncTearDown(self):
        for runtime in self.runtimes:await runtime.close()
        for workspace in self.workspaces:
            if not workspace.closed:await workspace.close()
        self.temp.cleanup()

    async def emit(self,event):self.events.append(event)

    def workspace(self,name='lesson'):
        session=TutorSession('synthetic',3,teacher_name='伴讀老師')
        workspace=CollaborationWorkspace(self.root/name,session,'basic')
        session.collaboration=workspace;self.workspaces.append(workspace)
        return session,workspace



    async def test_message_batch_rolls_back_on_later_publication_failure(self):
        session,workspace=self.workspace()
        workspace.append_message('老師','原有方向')
        before=(workspace.shared/'recent-messages.md').read_text(encoding='utf-8')
        before_index=workspace.message_index.read_text(encoding='utf-8')
        before_archives=set((workspace.shared/'messages').glob('*.md'))
        work=LessonWork(session,'新問題')
        with work.bind():
            workspace.append_message('老師','未採用問題')
            workspace.append_message('視覺夥伴','未採用答案')
            def fail():raise OSError('synthetic publication failure')
            work.stage('fail',fail,file=True)
        with self.assertRaises(OSError):work.commit()
        self.assertEqual((workspace.shared/'recent-messages.md').read_text(encoding='utf-8'),before)
        self.assertEqual(workspace.message_index.read_text(encoding='utf-8'),before_index)
        self.assertEqual(set((workspace.shared/'messages').glob('*.md')),before_archives)

    async def test_stale_or_closed_messages_never_write_current_or_archive(self):
        session,workspace=self.workspace();stamp=workspace.valid()
        session.interrupt({})
        self.assertFalse(workspace.append_message('視覺夥伴','舊答案',stamp))
        self.assertFalse((workspace.shared/'messages').exists())
        await workspace.close()
        self.assertFalse(workspace.append_message('老師','結束後的答案'))

    async def test_free_markdown_and_large_messages_keep_whole_limitations(self):
        _,workspace=self.workspace()
        for i in range(8):workspace.append_message('老師',f'問題 {i}\n### 自訂標題\n> 保留引用\n| 欄 | 值 |\n|---|---|\n| A | B |')
        text='前言\n'+('段落。'*3500)+'\n### 不確定處\n不能認定是九。'
        workspace.append_message('視覺夥伴',text)
        workspace.append_message('老師','稍後再核對。')
        self.assertIn(text,workspace.snapshot('teacher'))

    async def test_memory_withdrawal_does_not_resurrect_archived_messages(self):
        session,workspace=self.workspace()
        workspace.append_message('視覺夥伴','已撤回觀察')
        session.plan_revision+=1;workspace.invalidate_memory()
        workspace.append_message('老師','依新資料接續。')
        self.assertNotIn('已撤回觀察',workspace.snapshot('teacher'))
        again=CollaborationWorkspace(workspace.student_root,session,'basic');self.workspaces.append(again)
        again.append_message('老師','目前第三題。')
        self.assertNotIn('已撤回觀察',again.snapshot('teacher'))

    async def test_resuming_lesson_updates_shared_guidelines(self):
        session,workspace=self.workspace()
        atomic_text(workspace.shared/'team-guidelines.md','舊規則')
        again=CollaborationWorkspace(workspace.student_root,session,'basic');self.workspaces.append(again)
        self.assertEqual((again.shared/'team-guidelines.md').read_text(encoding='utf-8'),TEAM_GUIDELINES)
        self.assertIn('分歧與自主權',again.snapshot('vision'))

    async def test_waiting_synthesis_cannot_speak_after_parent_revision(self):
        session,workspace=self.workspace();started=asyncio.Event();finish=asyncio.Event()
        class SlowSpeech(Speech):
            async def synthesize(inner,text):started.set();await finish.wait();return {'audio':'','text':text,'boundaries':[]}
        runtime=BasicRuntime(session,CollaborativeProvider(NaturalProvider(),workspace,'teacher'),
            CollaborativeProvider(NaturalProvider(),workspace,'vision'),SlowSpeech(),self.emit,lambda *args:None)
        self.runtimes.append(runtime)
        work=LessonWork(session,'核對照片')
        with work.bind():task=asyncio.create_task(runtime.say_while_waiting('你想先問哪裡？',workspace.valid()))
        await started.wait();session.plan_revision+=1;finish.set();await task
        self.assertFalse(any(event['type']=='speech' for event in self.events))

    async def test_teacher_controls_waiting_prose_and_only_speaks_once(self):
        session,workspace=self.workspace()
        runtime=BasicRuntime(session,CollaborativeProvider(NaturalProvider(),workspace,'teacher'),
            CollaborativeProvider(NaturalProvider(),workspace,'vision'),Speech(),self.emit,lambda *args:None)
        self.runtimes.append(runtime)
        turn=workspace.role_path('teacher')/'drafts/waiting';turn.mkdir(parents=True)
        atomic_text(turn/'waiting.md','你覺得這題先找哪個條件比較合適？')
        work=LessonWork(session,'請核對')
        with work.bind():
            task=runtime.teacher.provider._start_waiting(turn,workspace.valid())
            await task
            self.assertIsNone(runtime.teacher.provider._start_waiting(turn,workspace.valid()))
        speech=[event for event in self.events if event['type']=='speech']
        self.assertEqual(len(speech),1)
        self.assertEqual(speech[0]['text'],'你覺得這題先找哪個條件比較合適？')

    async def test_teacher_can_choose_silent_waiting(self):
        _,workspace=self.workspace();workspace.waiting=lambda *args:None
        provider=CollaborativeProvider(NaturalProvider(),workspace,'teacher')
        turn=workspace.role_path('teacher')/'drafts/quiet';turn.mkdir(parents=True)
        atomic_text(turn/'waiting.md','')
        self.assertIsNone(provider._start_waiting(turn,workspace.valid()))

    def test_shell_helper_accepts_110_second_answer_within_work_deadline(self):
        root=self.root/'helper';turn=root/'drafts/token';turn.mkdir(parents=True)
        atomic_text(root/'internal/active-turn.json',json.dumps({'token':'token','role':'teacher','deadline':150}))
        atomic_text(turn/'question.md','請核對分母，保留擦痕的不確定性。')
        clock=[0]
        def step(_):
            clock[0]+=11
            if clock[0]>=110:
                request=next((turn/'requests').iterdir())
                atomic_text(request/'answer.md','分母像是四，但擦痕尚未確認。')
        with patch('server.collaboration_cli.time',SimpleNamespace(monotonic=lambda:clock[0],sleep=step)):
            result=ask(root,'token','question.md')
        self.assertIn('擦痕尚未確認',result)
        self.assertEqual(clock[0],110)










    def test_all_student_fields_and_accessible_board_labels_are_checked(self):
        for result in ({'speech':'我請另一個模型幫忙。','board_html':''},
                       {'speech':'我們先讀題。','board_html':'<img alt="視覺夥伴" />'},
                       {'speech':'我們先讀題。','board_html':'','lesson':{'tasks':[{'title':'視覺夥伴核對'}]}}):
            with self.assertRaises(ValueError):validate_student_output(result)
        validate_visible_text('先用數學模型理解這個情境。')


if __name__=='__main__':unittest.main()
