"""Synthetic integration tests: real file bridge, routing and restart boundaries."""
import asyncio
import base64
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from .basic import BasicRuntime
from .collaboration import CollaborationWorkspace, CollaborativeProvider
from .collaboration_cli import safe_path, atomic_text
from .lesson import empty_lesson
from .test_basic import Speech
from .test_support import SeededStore
from .tutor import TutorSession, DECISION_SCHEMA


def reply(speech='我們先讓每一份一樣大。', basic=False):
    value={'quiet':False,'speech':speech,'board_html':'','close_board':False,
           'need_capture':False,'evidence':[]}
    if basic:value['lesson']=empty_lesson()
    return json.dumps(value,ensure_ascii=False)


class NaturalProvider:
    def __init__(self,answer='第三題是 1/3＋1/4。右邊被手擋住，不能核對最後一步。'):
        self.answer=answer;self.calls=[];self.invalidations=0;self.closed=0
        self.conversation_generation=0
    def configure_role(self,role,file):self.role=role;self.file=file
    def invalidate_conversation(self):
        self.invalidations+=1;self.conversation_generation+=1
    async def turn(self,prompt,**kwargs):
        self.calls.append((prompt,kwargs))
        if 'interaction' in (kwargs.get('schema') or {}).get('required',[]):
            value=json.loads(self.answer)
            value.setdefault('interaction',{'mode':'unchanged','reason':''});value.setdefault('learning',[])
            value.setdefault('lesson',empty_lesson())
            return json.dumps(value,ensure_ascii=False)
        return self.answer
    async def close(self):self.closed+=1


class CollaborationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.session=TutorSession('synthetic',3,teacher_name='伴讀老師')
        self.workspace=CollaborationWorkspace(self.root,self.session,'basic')
        self.session.collaboration=self.workspace
        self.events=[];self.saved=[]
    async def asyncTearDown(self):self.temp.cleanup()
    async def emit(self,event):self.events.append(event)
    def runtime(self,teacher=None,vision=None):
        self.session.basic_workflow=True
        teacher=teacher or NaturalProvider(reply(basic=True))
        vision=vision or NaturalProvider()
        return BasicRuntime(self.session,CollaborativeProvider(teacher,self.workspace,'teacher'),
            CollaborativeProvider(vision,self.workspace,'vision'),Speech(),self.emit,
            lambda kind,payload:self.saved.append((kind,payload)),self.workspace.photos)

    async def test_natural_visual_evidence_is_shared_but_never_a_student_caption(self):
        vision=NaturalProvider('### 我看到的\n分母像是 4，但有一道擦痕；先不要認定是 9。')
        runtime=self.runtime(vision=vision)
        runtime.accept_frame('data:image/jpeg;base64,/9j/')
        await runtime.observations.task
        self.assertIsNone(vision.calls[0][1]['schema'])
        self.assertIsNone(vision.calls[0][1]['image_url'])
        self.assertIn('selected_photo.archive_path',vision.calls[0][0])
        self.assertIn('擦痕',self.workspace.snapshot('teacher'))
        self.assertFalse(any(e['type'] in ('speech','transcript','observation') for e in self.events))
        await runtime.handle_text('為什麼分母要變成十二？')
        call=runtime.teacher.provider.provider.calls[-1]
        self.assertIsNone(call[1]['image_url'])
        self.assertIn('擦痕',call[0])
        self.assertTrue(any(e['type']=='speech' for e in self.events))
        runtime.set_camera(False)
        self.assertNotIn('擦痕',self.workspace.snapshot('teacher'))
        self.assertFalse((self.workspace.role_path('teacher')/'internal/shared/latest-observation.md').exists())
        await runtime.close()

    async def test_all_modes_supply_current_progress_without_teacher_writing_notes(self):
        for mode in ('basic','gpt','gemini'):
            with self.subTest(mode=mode):
                session=TutorSession('synthetic',3,teacher_name='同一老師')
                workspace=CollaborationWorkspace(self.root/mode,session,mode)
                session.tasks=[{'id':'three','title':'分數第三題','status':'todo','evidence_quote':''}]
                session.plan_confirmed=True
                session.heard_history=[{'role':'assistant','text':'先找三與四的公倍數。'},
                    {'role':'user','text':'為什麼是十二？'}]
                native=NaturalProvider();vision=CollaborativeProvider(native,workspace,'vision')
                await vision.turn('請核對目前題目')
                prompt=native.calls[-1][0]
                self.assertIn('分數第三題：待完成',prompt)
                self.assertIn('為什麼是十二',prompt)
                self.assertIn('先找三與四',prompt)
                session.tasks[0]['status']='done';session.camera_enabled=False
                snapshot=workspace.snapshot('vision')
                self.assertIn('分數第三題：已記錄完成',snapshot)
                self.assertIn('鏡頭已關閉',snapshot)
                await vision.close()

    async def test_api_vision_has_context_but_no_phantom_file_tools(self):
        native=NaturalProvider();native.vision_only=True
        vision=CollaborativeProvider(native,self.workspace,'vision')
        await vision.turn('核對分母',image_url='data:image/png;base64,iVBORw0KGgo=')
        prompt=native.calls[-1][0]
        self.assertIn('沒有本機檔案、shell 或通知工具',prompt)
        self.assertIn('直接回覆自然語言觀察',prompt)
        self.assertNotIn('本回合草稿目錄',prompt)
        self.assertNotIn('new-context.md',prompt)
        self.assertNotIn('可寫 lesson.md',prompt)

    async def test_live_transcripts_are_not_presented_as_verified_playback(self):
        self.session.heard_history=[{'role':'assistant','text':'這句可能尚未播放',
            'status':'provider_transcript_not_playback_receipt'}]
        snapshot=self.workspace.snapshot('teacher')
        self.assertIn('Live 逐字稿，播放未核對',snapshot)
        self.assertNotIn('實際播放回報',snapshot)

    async def test_real_file_tool_delivers_free_form_question_and_answer(self):
        requests=[]
        async def inspect(question,fresh):
            requests.append((question,fresh));return '看得清楚的左側是 1/3。右側被遮住，請移手。'
        self.workspace.inspect=inspect
        class Teacher(NaturalProvider):
            async def turn(inner,prompt,**kwargs):
                token=json.loads((self.workspace.role_path('teacher')/'internal/active-turn.json').read_text())['token']
                draft=self.workspace.role_path('teacher')/'drafts'/token
                (draft/'question.md').write_text('右邊分母有擦痕；能再核對第三題嗎？\n必要時請重拍。',encoding='utf-8')
                process=await asyncio.create_subprocess_exec(sys.executable,'-X','utf8','internal/bridge.py',
                    '--turn',token,'ask-vision','--file','question.md','--fresh',
                    cwd=self.workspace.role_path('teacher'),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                output,errors=await asyncio.wait_for(process.communicate(),5)
                self.assertEqual(process.returncode,0,errors.decode())
                self.assertIn('請移手',output.decode('utf-8'))
                (draft/'lesson.md').write_text('先確認第三題，右側分母尚未確認。',encoding='utf-8')
                (draft/'notes.md').write_text('用每一份一樣大的例子引導。',encoding='utf-8')
                return reply()
        teacher=CollaborativeProvider(Teacher(),self.workspace,'teacher')
        await teacher.turn('請核對第三題後引導。',schema=DECISION_SCHEMA)
        self.assertEqual(len(requests),1);self.assertTrue(requests[0][1])
        self.assertIn('擦痕',requests[0][0])
        self.assertIn('請移手',(self.workspace.shared/'recent-messages.md').read_text(encoding='utf-8'))
        self.assertIn('尚未確認',(self.workspace.shared/'current-lesson.md').read_text(encoding='utf-8'))
        self.assertIn('每一份',(self.root/'collaboration/teacher-notes.md').read_text(encoding='utf-8'))
        await teacher.close()

    async def test_file_only_teacher_yields_and_resumes_without_shell_or_message_schema(self):
        questions=[]
        async def inspect(question,fresh):
            questions.append((question,fresh));return '右側分母是 4；最後一步仍被遮住。'
        self.workspace.inspect=inspect
        class Teacher(NaturalProvider):
            collaboration_tools='files'
            async def turn(inner,prompt,**kwargs):
                inner.calls.append(prompt)
                self.assertIsNone(kwargs['schema'])
                self.assertIn('只有核對完成後的最終介面回覆',prompt)
                if len(inner.calls)==1:
                    token=json.loads(inner.file.with_name('active-turn.json').read_text())['token']
                    draft=inner.file.parent.parent/'drafts'/token
                    (draft/'question.md').write_text('請再看看右邊，分母是不是四？\n有遮擋就說明。',encoding='utf-8')
                    return ''  # agy may finish a file-only step with no final text.
                self.assertIn('最後一步仍被遮住',prompt)
                return reply()
        native=Teacher();teacher=CollaborativeProvider(native,self.workspace,'teacher')
        self.assertEqual(json.loads(await teacher.turn('請教我',schema=DECISION_SCHEMA))['speech'],'我們先讓每一份一樣大。')
        self.assertEqual(len(native.calls),2)
        self.assertEqual(questions,[('請再看看右邊，分母是不是四？\n有遮擋就說明。',False)])
        self.assertNotIn('internal/bridge.py --turn',native.calls[0])

    async def test_invalid_or_internal_student_reply_cannot_commit_drafts(self):
        class Teacher(NaturalProvider):
            async def turn(inner,*args,**kwargs):
                token=json.loads(inner.file.with_name('active-turn.json').read_text())['token']
                atomic_text(inner.file.parent.parent/'drafts'/token/'lesson.md','不能發布')
                return reply('我請視覺夥伴幫你看看。')
        native=Teacher();teacher=CollaborativeProvider(native,self.workspace,'teacher')
        with self.assertRaises(ValueError):await teacher.turn('看圖',schema=DECISION_SCHEMA)
        self.assertNotIn('不能發布',(self.workspace.shared/'current-lesson.md').read_text(encoding='utf-8'))
        self.assertGreater(native.invalidations,0)

    async def test_cancelled_native_turn_cannot_commit_even_if_it_ignores_cancel(self):
        entered=asyncio.Event()
        class Late(NaturalProvider):
            async def turn(inner,prompt,**kwargs):
                token=json.loads(inner.file.with_name('active-turn.json').read_text())['token']
                draft=inner.file.parent.parent/'drafts'/token
                entered.set()
                try:await asyncio.sleep(10)
                except asyncio.CancelledError:pass
                (draft/'lesson.md').write_text('晚到的錯誤理解',encoding='utf-8')
                (draft/'notes.md').write_text('不應採用',encoding='utf-8')
                return reply()
        native=Late();teacher=CollaborativeProvider(native,self.workspace,'teacher')
        before=(self.workspace.shared/'current-lesson.md').read_text(encoding='utf-8')
        task=asyncio.create_task(teacher.turn('等待',schema=DECISION_SCHEMA))
        await entered.wait();self.session.interrupt({});task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        self.assertEqual((self.workspace.shared/'current-lesson.md').read_text(encoding='utf-8'),before)
        self.assertFalse((self.root/'collaboration/teacher-notes.md').exists())
        self.assertGreater(native.invalidations,0)

    async def test_inflight_photo_survives_routine_new_frame(self):
        entered=asyncio.Event();release=asyncio.Event()
        class Delayed(NaturalProvider):
            async def turn(inner,*args,**kwargs):entered.set();await release.wait();return '舊題目'
        native=Delayed();runtime=self.runtime(vision=native)
        runtime.accept_frame('data:image/jpeg;base64,/9j/')
        await entered.wait()
        runtime.accept_frame('data:image/jpeg;base64,/9j/AQ==')
        runtime.observations.pending=None
        release.set();await runtime.observations.task
        self.assertEqual(self.session.observation['text'],'舊題目')
        self.assertTrue((self.workspace.shared/'latest-observation.md').exists())
        self.assertEqual(native.invalidations,0)

        await runtime.close()

    async def test_directed_question_survives_routine_photo_update_with_capture_disclosure(self):
        entered=asyncio.Event();release=asyncio.Event()
        class Delayed(NaturalProvider):
            async def turn(inner,*args,**kwargs):entered.set();await release.wait();return '指定第三題的分母是四。'
        runtime=self.runtime(vision=Delayed())
        runtime.observations.push=lambda frame:None
        runtime.accept_frame('data:image/jpeg;base64,/9j/')
        selected=runtime.frame['id']
        task=asyncio.create_task(runtime.inspect_photo('請核對第三題分母'))
        await entered.wait()
        runtime.accept_frame('data:image/jpeg;base64,/9j/AQ==')
        release.set();answer=await task
        self.assertIn(selected,answer)
        self.assertIn('新照片待辨識',answer)
        self.assertEqual(self.session.observation['capture_id'],selected)
        self.assertIn('較早',self.workspace.snapshot('teacher'))
        self.assertEqual(runtime.directed_inspections,0)
        await runtime.close()
        await runtime.close()

    async def test_restore_recovers_confirmed_work_but_never_old_camera(self):
        self.session.tasks=[{'id':'one','title':'第三題','status':'todo','evidence_quote':''}]
        self.session.plan_confirmed=True
        self.session.heard_history=[{'role':'user','text':'十二'}]
        self.workspace.record_observation({'id':'old','received':time.monotonic()},'舊圖',self.workspace.valid())
        self.workspace.checkpoint(immediate=True)
        restored=TutorSession('synthetic',3,id=self.session.id)
        workspace=CollaborationWorkspace(self.root,restored,'basic');workspace.restore()
        self.assertEqual(restored.tasks,self.session.tasks)
        self.assertTrue(restored.plan_confirmed)
        self.assertEqual(restored.heard_history,self.session.heard_history)
        self.assertGreater(restored.generation,self.session.generation)
        self.assertIsNone(restored.observation)
        self.assertNotIn('舊圖',workspace.snapshot('teacher'))

    async def test_memory_withdrawal_and_turn_rotation(self):
        native=NaturalProvider();vision=CollaborativeProvider(native,self.workspace,'vision')
        for _ in range(41):await vision.turn('核對')
        self.assertEqual(native.invalidations,1)
        atomic_text(self.root/'collaboration/teacher-notes.md','家長已撤回的推測')
        self.workspace.invalidate_memory()
        self.assertNotIn('撤回的推測',self.workspace.snapshot('teacher'))
        self.session.plan_revision+=1;await vision.turn('新題')
        self.assertEqual(native.invalidations,2)

    async def test_team_guidelines_and_message_history_persistence(self):
        # Verify team guidelines exist and are shared
        guidelines_path = self.workspace.shared / 'team-guidelines.md'
        self.assertTrue(guidelines_path.exists())
        guidelines_text = guidelines_path.read_text(encoding='utf-8')
        self.assertIn('單一老師體驗', guidelines_text)
        self.assertIn('非同步與慢回覆容錯', guidelines_text)
        self.assertIn('自由格式交流', guidelines_text)
        snapshot = self.workspace.snapshot('teacher')
        self.assertIn('team-guidelines.md', snapshot)
        self.assertIn('團隊協作與伴讀準則', snapshot)

        # Verify append_message maintains history without overwriting
        self.workspace.append_message('老師', '這是第一個問題：請看第一題分子')
        self.workspace.append_message('視覺夥伴', '第一題分子是 3')
        self.workspace.append_message('老師', '這是第二個問題：請看第二題分母')
        self.workspace.append_message('視覺夥伴', '第二題分母是 7')

        recent = (self.workspace.shared / 'recent-messages.md').read_text(encoding='utf-8')
        self.assertIn('這是第一個問題', recent)
        self.assertIn('第一題分子是 3', recent)
        self.assertIn('這是第二個問題', recent)
        self.assertIn('第二題分母是 7', recent)

        # Verify individual message files exist in shared/messages
        messages = list((self.workspace.shared / 'messages').glob('*.md'))
        self.assertGreaterEqual(len(messages), 4)

    async def test_corrupt_checkpoint_cannot_replace_lesson_state(self):
        self.workspace.checkpoint(immediate=True)
        path=self.workspace.root/'internal/checkpoint.json'
        saved=json.loads(path.read_text(encoding='utf-8'))
        saved['tasks']=[{'id':'one','title':'第三題','status':'imagined'}]
        atomic_text(path,json.dumps(saved))
        with self.assertRaises(ValueError):self.workspace.restore()
        self.assertEqual(self.session.tasks,[])

    async def test_legacy_notes_are_available_once_and_withdrawn_after_correction(self):
        atomic_text(self.root/'basic-shared/teacher/lesson.md','舊版分數筆記')
        workspace=CollaborationWorkspace(self.root,self.session,'basic')
        self.assertIn('舊版分數筆記',workspace.snapshot('teacher'))
        workspace.invalidate_memory()
        self.assertNotIn('舊版分數筆記',workspace.snapshot('teacher'))
        restarted=CollaborationWorkspace(self.root,self.session,'basic')
        self.assertNotIn('舊版分數筆記',restarted.snapshot('teacher'))

    async def test_crashed_turn_invalidates_native_resume(self):
        path=self.workspace.role_path('teacher')
        atomic_text(path/'internal/active-turn.json',json.dumps({'token':'unfinished','role':'teacher'}))
        native=NaturalProvider();CollaborativeProvider(native,self.workspace,'teacher')
        self.assertEqual(native.invalidations,1)

    async def test_teacher_rejects_original_images_and_external_paths(self):
        teacher=CollaborativeProvider(NaturalProvider(),self.workspace,'teacher')
        with self.assertRaises(ValueError):await teacher.turn('看圖',image_url='data:image/jpeg;base64,/9j/')
        with self.assertRaises(ValueError):safe_path(self.workspace.root,'../../private')
        with self.assertRaises(ValueError):safe_path(self.workspace.root,str(self.root/'other.txt'))
        self.assertFalse((self.workspace.role_path('teacher')/'internal/photos').exists())

    async def test_png_archive_keeps_original_bytes_and_immutable_paths(self):
        data=b'\x89PNG\r\n\x1a\n'+bytes(4_000_000)
        first=await self.workspace.photos.publish('data:image/png;base64,'+base64.b64encode(data).decode())
        second=await self.workspace.photos.publish('data:image/jpeg;base64,/9j/')
        self.assertNotEqual(first['archive_path'],second['archive_path'])
        self.assertEqual((self.workspace.photos.root/first['archive_path']).read_bytes(),data)


class LessonResumeTests(unittest.IsolatedAsyncioTestCase):

    async def test_resume_cannot_cross_student_or_reopen_finalization(self):
        with tempfile.TemporaryDirectory() as directory:
            store=SeededStore(Path(directory));sid=store.start('student-2','basic','')
            store.event('student-2',sid,'session_config',{'backend':'codex'})
            store.finish('student-2',sid,'interrupted')
            with self.assertRaises(ValueError):store.resume('student-4',sid)
            store.resume('student-2',sid)
            store.begin_finalization('student-2',sid,False,'codex')
            store.finish('student-2',sid,'interrupted')
            with self.assertRaises(ValueError):store.resume('student-2',sid)
            self.assertIsNone(store.resumable('student-2',sid))


    async def test_append_message_within_work_context_retains_all_messages(self):
        from .work_lifecycle import LessonWork
        with tempfile.TemporaryDirectory() as folder:
            session = TutorSession('test-work-messages', 3)
            ws = CollaborationWorkspace(folder, session, 'test')
            work = LessonWork(session, 'test-request')
            with work.bind():
                ws.append_message('老師', '問題一：這是加法嗎？')
                ws.append_message('視覺夥伴', '回答一：這是加法沒錯。')
                work.commit()
            recent = (ws.shared / 'recent-messages.md').read_text(encoding='utf-8')
            self.assertIn('問題一：這是加法嗎？', recent)
            self.assertIn('回答一：這是加法沒錯。', recent)
            snapshot = ws.snapshot('teacher')
            self.assertIn('問題一：這是加法嗎？', snapshot)
            self.assertIn('回答一：這是加法沒錯。', snapshot)
            await ws.close()

if __name__=='__main__':unittest.main()
