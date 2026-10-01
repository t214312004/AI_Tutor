import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path

from .basic import BasicRuntime
from .basic_workspace import BasicWorkspace,prepare_student_workspace
from .lesson import empty_lesson
from .test_basic import Speech
from .tutor import TutorSession


class RecordingTutor:
    def __init__(self):self.calls=[];self.closed=0
    async def turn(self,prompt,**kwargs):
        self.calls.append((prompt,kwargs))
        if kwargs['schema']['required']==['evidence','signal','focus','confidence']:
            return json.dumps({'evidence':['測試算式'],'signal':'none','focus':'','confidence':'low'})
        return json.dumps({'quiet':False,'speech':'試著先看個位數。','board_html':'',
                           'close_board':False,'need_capture':False,'evidence':[],
                           'lesson':empty_lesson()})
    async def close(self):self.closed+=1


class BasicWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_transport_pause_preserves_latest_photo(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace=BasicWorkspace(Path(folder));provider=RecordingTutor()
            async def emit(event):pass
            runtime=BasicRuntime(TutorSession('synthetic',2,basic_workflow=True),
                                 provider,provider,Speech(),emit,lambda *args:None,workspace)
            image='data:image/jpeg;base64,'+base64.b64encode(b'\xff\xd8\xfflast-frame').decode()
            runtime.accept_frame(image)
            await asyncio.gather(*runtime.photo_tasks)
            runtime.set_camera(False,preserve_photo=True)
            self.assertTrue(workspace.photo.exists())
            self.assertTrue(workspace.metadata.exists())
            self.assertIsNone(runtime.frame)
            runtime.set_camera(True)
            runtime.set_camera(False)
            await asyncio.gather(*runtime.photo_tasks)
            self.assertTrue(workspace.photo.exists())
            await runtime.close()
            self.assertTrue(workspace.photo.exists())

    async def test_photo_is_replaced_in_workspace_and_only_notice_enters_prompt(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace=BasicWorkspace(Path(folder))
            provider=RecordingTutor();events=[]
            async def emit(event):events.append(event)
            session=TutorSession('synthetic',2,basic_workflow=True)
            runtime=BasicRuntime(session,provider,provider,Speech(),emit,lambda *args:None,workspace)
            image='data:image/jpeg;base64,'+base64.b64encode(b'\xff\xd8\xffsynthetic-frame').decode()
            runtime.accept_frame(image)
            await runtime.observations.task
            self.assertTrue((Path(folder)/'AGENTS.md').exists())
            self.assertEqual(workspace.photo.read_bytes(),b'\xff\xd8\xffsynthetic-frame')
            metadata=json.loads(workspace.metadata.read_text(encoding='utf-8'))
            self.assertEqual(metadata['version'],1)
            self.assertEqual(metadata['capture_id'],runtime.frame['id'])
            self.assertIn('captured_at',metadata)
            self.assertEqual((workspace.root/metadata['archive_path']).read_bytes(),b'\xff\xd8\xffsynthetic-frame')
            observation,options=provider.calls[0]
            self.assertIsNone(options['image_url'])
            self.assertNotIn(image,observation)
            self.assertIn('photo_updated',observation)
            observed_context=json.loads(observation.split('資料：',1)[1])
            self.assertEqual(observed_context['selected_photo']['archive_path'],metadata['archive_path'])
            self.assertEqual(options['schema']['required'],['evidence','signal','focus','confidence'])
            runtime.accept_text('請給提示')
            await asyncio.gather(*runtime.voice_tasks)
            teaching,_=provider.calls[1]
            context=json.loads(teaching.split('資料：',1)[1])
            self.assertNotIn('photo_updated',context)
            self.assertEqual(context['selected_photo']['archive_path'],metadata['archive_path'])
            self.assertNotIn('previous_sessions',context)
            self.assertNotIn('lesson_state',context)
            self.assertNotIn('new_heard_turns',context)
            runtime.set_camera(False)
            await asyncio.gather(*runtime.photo_tasks)
            self.assertTrue(workspace.photo.exists())
            self.assertTrue((Path(folder)/'AGENTS.md').exists())
            await runtime.close()
            self.assertEqual(provider.closed,1)
            self.assertTrue(workspace.photo.exists())
            self.assertTrue((Path(folder)/'AGENTS.md').exists())

    async def test_new_photo_does_not_discard_timely_observation_of_archived_photo(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace=BasicWorkspace(Path(folder));events=[]
            entered=asyncio.Event();release=asyncio.Event()
            class SlowTutor(RecordingTutor):
                async def turn(self,prompt,**kwargs):
                    if kwargs['schema']['required']==['evidence','signal','focus','confidence'] and not entered.is_set():
                        entered.set();await release.wait()
                    return await super().turn(prompt,**kwargs)
            provider=SlowTutor()
            async def emit(event):events.append(event)
            runtime=BasicRuntime(TutorSession('synthetic',2,basic_workflow=True),
                                 provider,provider,Speech(),emit,lambda *args:None,workspace)
            def image(label):
                return 'data:image/jpeg;base64,'+base64.b64encode(b'\xff\xd8\xff'+label).decode()
            runtime.accept_frame(image(b'first'))
            await entered.wait()
            first_task=runtime.observations.task
            runtime.accept_frame(image(b'second'))
            await asyncio.gather(*runtime.photo_tasks)
            self.assertEqual(workspace.version,2)
            release.set();await first_task
            observations=[event for event in events if event['type']=='observation']
            self.assertEqual(len(observations),2)
            notices=[json.loads(call[0].split('資料：',1)[1])['selected_photo']
                     for call in provider.calls if call[1]['schema']['required']==['evidence','signal','focus','confidence']]
            self.assertNotEqual(notices[0]['archive_path'],notices[1]['archive_path'])
            self.assertTrue(all((workspace.root/notice['archive_path']).is_file() for notice in notices))
            await runtime.close()

    async def test_context_sends_only_new_heard_turns_and_changed_lesson_state(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace=BasicWorkspace(Path(folder));provider=RecordingTutor()
            async def emit(event):pass
            session=TutorSession('synthetic',2,basic_workflow=True)
            session.memory=[{'summary':'上堂課的摘要'}]
            runtime=BasicRuntime(session,provider,provider,Speech(),emit,lambda *args:None,workspace)
            await runtime.teacher.respond(session,'開場')
            initial=json.loads(provider.calls[-1][0].split('資料：',1)[1])
            self.assertEqual(initial['previous_sessions'],session.memory)
            self.assertIn('workspace_continuity',initial)
            session.heard_history.append({'role':'assistant','text':'實際播放一半'})
            await runtime.teacher.respond(session,'下一題')
            second=json.loads(provider.calls[-1][0].split('資料：',1)[1])
            self.assertEqual(second['new_heard_turns'],[{'role':'assistant','text':'實際播放一半'}])
            self.assertNotIn('previous_sessions',second)
            self.assertNotIn('lesson_state',second)
            session.tasks=[{'id':'math','title':'數學第 1 題','status':'todo','evidence_quote':''}]
            await runtime.update_lesson('confirm')
            await runtime.teacher.respond(session,'接下來呢')
            third=json.loads(provider.calls[-1][0].split('資料：',1)[1])
            self.assertTrue(third['lesson_state']['confirmed'])
            self.assertNotIn('new_heard_turns',third)
            await runtime.close()

    async def test_all_photos_and_notes_survive_new_workspace_instance(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            first=BasicWorkspace(root)
            images=[b'\xff\xd8\xfffirst',b'\xff\xd8\xffsecond']
            notices=[]
            for image in images:
                notices.append(await first.publish('data:image/jpeg;base64,'+base64.b64encode(image).decode()))
            (root/'teacher-notes.txt').write_text('從十位數開始',encoding='utf-8')
            await first.close()
            second=BasicWorkspace(root)
            self.assertEqual(second.version,2)
            self.assertEqual(second.photo.read_bytes(),images[-1])
            self.assertEqual((root/'teacher-notes.txt').read_text(encoding='utf-8'),'從十位數開始')
            self.assertEqual([((root/notice['archive_path']).read_bytes()) for notice in notices],images)
            third=await second.publish('data:image/jpeg;base64,'+base64.b64encode(b'\xff\xd8\xffthird').decode())
            self.assertEqual(third['version'],3)
            self.assertEqual(len(list(second.photos.glob('*.jpg'))),3)

    async def test_legacy_session_files_are_available_in_shared_workspace(self):
        with tempfile.TemporaryDirectory() as folder:
            student=Path(folder)
            old=student/('a'*32)/'teacher'
            old.mkdir(parents=True)
            (old/'notes.txt').write_text('舊課筆記',encoding='utf-8')
            (old/'internal').mkdir()
            (old/'internal'/'latest-photo.jpg').write_bytes(b'\xff\xd8\xffold')
            shared=prepare_student_workspace(student)
            self.assertEqual((shared/'prior-sessions'/('a'*32)/'notes.txt').read_text(encoding='utf-8'),'舊課筆記')
            self.assertEqual((shared/'prior-sessions'/('a'*32)/'internal'/'latest-photo.jpg').read_bytes(),b'\xff\xd8\xffold')
            self.assertTrue((old/'notes.txt').exists())


if __name__=='__main__':unittest.main()
