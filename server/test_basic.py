import asyncio
import json
import unittest
import time
from unittest.mock import patch
from .basic import BasicRuntime,LatestObservation,SpeechService,sanitize_transcript
from .tutor import TutorEngine,TutorSession

class Teacher:
    async def turn(self,*args,**kwargs):
        if kwargs.get('schema',{}).get('required')==['evidence','signal','focus','confidence']:
            return json.dumps({'evidence':[],'signal':'none','focus':'','confidence':'low'})
        result={'quiet':False,'speech':'先想想十減七是多少。','board_html':'<p>7 + □ = 10</p>','close_board':False,'need_capture':False,'evidence':[]}
        if 'lesson' in kwargs.get('schema',{}).get('properties',{}):
            from .lesson import empty_lesson
            result['lesson']=empty_lesson()
        return json.dumps(result)
class Speech:
    async def transcribe(self,pcm):return '七加五怎麼算？'
    async def synthesize(self,text):return {'audio':'','boundaries':[],'text':text}

class BasicTests(unittest.IsolatedAsyncioTestCase):
    async def test_stt_does_not_seed_teacher_sentence_and_rejects_legacy_echo(self):
        calls=[]
        class Response:
            is_success=True
            def json(self):return {'text':'使用者會稱呼老師叫小優老師。',
                                    'segments':[{'no_speech_prob':0.2}]}
        class Client:
            def __init__(self,**kwargs):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def post(self,*args,**kwargs):
                calls.append(kwargs['data']);return Response()
        with patch('server.basic.httpx.AsyncClient',Client):
            result=await SpeechService('synthetic',teacher_name='小優老師').transcribe(b'\0\0'*16000)
        self.assertNotIn('prompt',calls[0])
        self.assertEqual(result['text'],'')
        self.assertTrue(result['quality']['rejected'])

    async def test_invalid_teaching_json_retries_once_without_applying_first_reply(self):
        class Flaky(Teacher):
            def __init__(self):self.prompts=[]
            async def turn(self,prompt,*args,**kwargs):
                self.prompts.append(prompt)
                if len(self.prompts)==1:return '{invalid json'
                return await super().turn(prompt,*args,**kwargs)
        provider=Flaky();engine=TutorEngine(provider)
        session=TutorSession('synthetic',2,basic_workflow=True)
        result=await engine.respond(session,'七加五怎麼想？')
        self.assertEqual(result['speech'],'先想想十減七是多少。')
        self.assertEqual(len(provider.prompts),2)
        self.assertIn('上一個回覆未通過 JSON 格式驗證',provider.prompts[1])
        self.assertEqual(session.tasks,[])

    async def test_restarted_provider_receives_full_lesson_context(self):
        class Restartable(Teacher):
            conversation_generation=0
            prompts=[]
            async def ensure_conversation(self):pass
            async def turn(self,text,*args,**kwargs):
                self.prompts.append(text)
                return await super().turn(text,*args,**kwargs)
        provider=Restartable();engine=TutorEngine(provider)
        session=TutorSession('synthetic',2,'合成課程',basic_workflow=True)
        await engine.respond(session,'開始上課')
        provider.conversation_generation=1
        await engine.respond(session,'繼續上課')
        second=json.loads(provider.prompts[-1].split('資料：',1)[1])
        self.assertEqual(second['grade'],2)
        self.assertEqual(second['session_notes'],'合成課程')

    def make(self):
        self.events=[];self.saved=[]
        async def emit(event):self.events.append(event)
        return BasicRuntime(TutorSession('synthetic',2),Teacher(),Teacher(),Speech(),emit,lambda *args:self.saved.append(args))
    async def test_full_turn_records_only_acknowledged_playback(self):
        runtime=self.make();runtime.accept_audio(b'\0\0'*16000)
        await asyncio.gather(*runtime.voice_tasks)
        self.assertEqual([e['type'] for e in self.events],
                         ['audio_status','audio_status','transcript','thinking','board','speech'])
        self.assertEqual([e['stage'] for e in self.events[:2]],['transcribing','accepted'])
        self.assertEqual(len(runtime.session.heard_history),1)
        event=self.events[-1];runtime.played(event['playback_id'],{'fully_heard':'先想想'},False)
        self.assertEqual(runtime.session.heard_history[-1]['text'],'先想想')
        self.assertNotIn('十減七',runtime.session.heard_history[-1]['text'])
        cost=await runtime.close();self.assertAlmostEqual(cost['groq_estimate_usd'],10*.111/3600)
    async def test_muted_audio_is_never_transcribed(self):
        runtime=self.make();runtime.set_mic(False);runtime.accept_audio(b'\0\0')
        self.assertEqual(len(runtime.voice_tasks),0);await runtime.close()
    async def test_demand_capture_bypasses_busy_observer(self):
        runtime=self.make();task=asyncio.create_task(runtime.capture_now());await asyncio.sleep(0)
        request=self.events[-1]['request_id'];runtime.accept_frame('data:image/jpeg;base64,/9j/',request)
        frame=await task;self.assertIsNotNone(frame);self.assertIsNone(runtime.observations.task);await runtime.close()
    async def test_latest_observation_replaces_pending_only(self):
        seen=[];release=asyncio.Event()
        async def work(frame):seen.append(frame);await release.wait()
        async def error(message):self.fail(message)
        queue=LatestObservation(work,error);queue.push(1);await asyncio.sleep(0);queue.push(2);queue.push(3)
        self.assertEqual(queue.skipped,1);release.set();await queue.task
        self.assertEqual(seen,[1,3]);await queue.close()
    async def test_shared_provider_closes_once_and_student_turn_preempts_observation(self):
        started=asyncio.Event();cancelled=asyncio.Event();calls=[]
        class Shared(Teacher):
            async def turn(self,text,*args,**kwargs):
                calls.append(text)
                if '觀察只回報證據' in text:
                    started.set()
                    try:await asyncio.Future()
                    except asyncio.CancelledError:
                        cancelled.set();raise
                return await super().turn(text,*args,**kwargs)
            async def close(self):self.closed=getattr(self,'closed',0)+1
        shared=Shared();events=[]
        async def emit(event):events.append(event)
        runtime=BasicRuntime(TutorSession('synthetic',2),shared,shared,Speech(),emit,lambda *args:None)
        self.assertIs(runtime.teacher,runtime.observer)
        runtime.accept_frame('data:image/jpeg;base64,/9j/')
        await started.wait()
        runtime.accept_text('這一題怎麼做？')
        await cancelled.wait()
        await asyncio.gather(*runtime.voice_tasks)
        self.assertTrue(any(e['type']=='speech' for e in events))
        self.assertEqual(len(calls),2)
        await runtime.close()
        self.assertEqual(shared.closed,1)
    async def test_prompt_marks_camera_and_excludes_stale_observation(self):
        class Recorder(Teacher):
            prompt=''
            async def turn(self,text,*args,**kwargs):
                self.prompt=text
                return await super().turn(text,*args,**kwargs)
        runtime=self.make();runtime.session.basic_workflow=True
        recorder=Recorder();runtime.teacher.provider=recorder
        runtime.session.observation={'evidence':['舊題目'],'received':time.monotonic()-31,'capture_id':'old'}
        with patch('server.tutor.teaching_references',return_value=[]) as references:
            await runtime.teacher.respond(runtime.session,'請提示')
            self.assertEqual(references.call_args.args[4],[])
        context=json.loads(recorder.prompt.split('資料：',1)[1])
        self.assertTrue(context['camera_enabled'])
        self.assertNotIn('latest_observation',context)
        runtime.session.observation={'evidence':['目前題目'],'received':time.monotonic()-2,'capture_id':'fresh'}
        with patch('server.tutor.teaching_references',return_value=[]) as references:
            await runtime.teacher.respond(runtime.session,'請提示',image_url='data:image/jpeg;base64,AA==',image_age_seconds=3)
            self.assertEqual(references.call_args.args[4],['目前題目'])
        context=json.loads(recorder.prompt.split('資料：',1)[1])
        self.assertNotIn('latest_observation',context)
        self.assertTrue(context['current_photo_available'])
        self.assertEqual(context['current_photo_age_seconds'],3)
        runtime.set_camera(False)
        await runtime.teacher.respond(runtime.session,'請提示')
        context=json.loads(recorder.prompt.split('資料：',1)[1])
        self.assertFalse(context['camera_enabled'])
        self.assertNotIn('latest_observation',context)
        self.assertIsNone(runtime.session.observation)
        await runtime.close()
    async def test_end_invalidates_late_tts(self):
        runtime=self.make();entered=asyncio.Event();release=asyncio.Event()
        async def slow(text):
            entered.set()
            try:await release.wait()
            except asyncio.CancelledError:pass
            return {'audio':'','text':text,'boundaries':[]}
        runtime.speech.synthesize=slow;runtime.accept_audio(b'\0\0'*16000);await entered.wait();await runtime.close()
        self.assertFalse(any(e['type']=='speech' for e in self.events))

    async def test_camera_off_while_photo_publishes_excludes_late_photo(self):
        class Photo:
            def __init__(self):self.entered=asyncio.Event();self.release=asyncio.Event()
            async def publish(self):
                self.entered.set();await self.release.wait()
                return {'version':1}
            async def clear_photo(self):pass
            async def close(self):pass

        runtime=self.make();photo=Photo();runtime.photo_workspace=photo
        photo_task=asyncio.create_task(photo.publish())
        runtime.frame={'id':'late','image':'data:image/jpeg;base64,AA==',
                       'camera_epoch':0,'received':time.monotonic(),'photo_task':photo_task}
        seen=[]
        async def respond(*args,**kwargs):
            seen.append(kwargs)
            return {'need_capture':False,'speech':'','board_html':'','close_board':False,
                    'curriculum_candidates':[],'lesson':{'tasks':[]}}
        runtime.teacher.respond=respond
        teaching=asyncio.create_task(runtime.teach('開場',runtime.session.generation,internal=True))
        await photo.entered.wait()
        runtime.set_camera(False)
        photo.release.set()
        await teaching
        self.assertIsNone(seen[0]['image_url'])
        self.assertIsNone(seen[0]['photo_notice'])
        await runtime.close()

    async def test_camera_off_discards_reply_based_on_inflight_photo(self):
        runtime=self.make();entered=asyncio.Event();release=asyncio.Event()
        runtime.frame={'id':'photo','image':'data:image/jpeg;base64,AA==',
                       'camera_epoch':0,'received':time.monotonic()}
        async def respond(*args,**kwargs):
            entered.set();await release.wait()
            return {'need_capture':False,'speech':'照片中的題目','board_html':'<p>照片</p>',
                    'close_board':False,'curriculum_candidates':[],'lesson':{'tasks':[]}}
        runtime.teacher.respond=respond
        teaching=asyncio.create_task(runtime.teach('請幫我',runtime.session.generation))
        await entered.wait()
        runtime.set_camera(False)
        release.set();await teaching
        self.assertFalse(any(e['type'] in ('speech','board') for e in self.events))
        await runtime.close()

    async def test_explicit_help_records_concept_without_mastery_claim(self):
        runtime=self.make();runtime.session.basic_workflow=True
        ref={'code':'N-1-3','subject':'數學','kind':'學習內容','grade_min':1,'grade_max':1,
             'summary':'基本加減法：以操作活動為主','source_title':'數學課綱',
             'page':22,'source_url':'https://example.test/math','quality':'auto_math_row'}
        with patch('server.tutor.teaching_references',return_value=[ref]):
            runtime.accept_text('七加五怎麼算？')
            await asyncio.gather(*runtime.voice_tasks)
        evidence=[payload for kind,payload in self.saved if kind=='learning_evidence']
        self.assertEqual(len(evidence),1)
        self.assertEqual((evidence[0]['code'],evidence[0]['kind']),('N-1-3','asked_for_help'))
        self.assertNotIn('mastery',evidence[0])
        await runtime.close()

    async def test_repeated_high_confidence_observation_records_once(self):
        runtime=self.make();runtime.session.basic_workflow=True
        calls=0
        async def observe(*args,**kwargs):
            nonlocal calls
            calls+=1
            return {'evidence':['7＋5 = ?'] if calls==1 else ['7＋5 的下一步寫出 10＋2'],
                    'lesson':{'signal':'none' if calls==1 else 'completed_step','focus':'7＋5','confidence':'high'}}
        runtime.observer.respond=observe
        ref={'code':'N-1-3','subject':'數學','summary':'基本加減法：操作活動', 'page':22}
        with patch('server.basic.teaching_references',return_value=[ref]):
            for index in range(3):
                frame={'id':str(index),'image':'data:image/jpeg;base64,AA==',
                       'camera_epoch':0,'received':time.monotonic()}
                await runtime.observe(frame)
                if index==1:runtime.signal_since-=11
        evidence=[payload for kind,payload in self.saved if kind=='learning_evidence']
        self.assertEqual(len(evidence),1)
        self.assertEqual(evidence[0]['kind'],'observed_step')
        await runtime.close()

    def test_sanitize_transcript_filters_hallucinations_and_high_no_speech(self):
        self.assertEqual(sanitize_transcript('Thank You.'), '')
        self.assertEqual(sanitize_transcript('Thank you for watching.'), '')
        self.assertEqual(sanitize_transcript('Thanks for watching!'), '')
        self.assertEqual(sanitize_transcript('中文字幕志愿者 李宗盛'), '')
        self.assertEqual(sanitize_transcript('謝謝收看。'), '')
        self.assertEqual(sanitize_transcript('請訂閱我們的頻道！'), '')
        self.assertEqual(sanitize_transcript('...'), '')
        self.assertEqual(sanitize_transcript('這題要怎麼算？', [{'no_speech_prob': 0.85}]), '')
        self.assertEqual(sanitize_transcript('這題要怎麼算？', [{'no_speech_prob': 0.002}]), '這題要怎麼算？')
        self.assertEqual(sanitize_transcript('使用者會稱呼老師叫小優老師。'), '')

    async def test_empty_transcription_from_noise_produces_no_events(self):
        class SilentSpeech:
            async def transcribe(self, pcm): return ''
            async def synthesize(self, text): return {'audio': '', 'boundaries': [], 'text': text}
        events = []
        async def emit(event): events.append(event)
        runtime = BasicRuntime(TutorSession('synthetic', 2), Teacher(), Teacher(), SilentSpeech(), emit, lambda *args: None)
        runtime.accept_audio(b'\0\0' * 16000)
        await asyncio.gather(*runtime.voice_tasks)
        self.assertEqual([e['stage'] for e in events],['transcribing','ignored'])
        self.assertEqual(len(runtime.session.heard_history), 0)
        await runtime.close()

if __name__=='__main__':unittest.main()
