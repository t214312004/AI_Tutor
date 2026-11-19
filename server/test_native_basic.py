import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from .native_basic import NativeBasicRuntime
from .classroom_runtime import ClassroomRuntime
from .tutor import TutorSession
from .lesson import empty_lesson
from .codex_voice import NATIVE_VOICES


class FakeClient:
    thread_id = 'primary'
    def __init__(self):
        self.calls = []
        self.closed = False
    async def request(self, method, params, **kwargs):
        self.calls.append((method, params))
        if method=='thread/realtime/stop':
            self.notification('thread/realtime/closed',{'threadId':self.thread_id})
        if method=='thread/realtime/listVoices':return {'voices':{'v1':list(NATIVE_VOICES)}}
        return {'turn': {'id': 'typed'}}
    async def close(self):
        self.closed = True
    async def ensure_ready(self, instructions, tools):
        self.instructions=instructions
        self.tools=tools


class NativeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.client = FakeClient()
        self.observer = type('Observer', (), {'workspace': Path(self.folder.name),
            'close': AsyncMock(), 'turn': AsyncMock(), 'vision_only': True})()
        self.session = TutorSession('synthetic', 2, basic_workflow=True, input_speaker='student')
        self.events, self.records = [], []
        async def emit(event):self.events.append(event)
        self.runtime = NativeBasicRuntime(self.session, self.observer, emit,
            lambda kind, data:self.records.append((kind, data)), client=self.client)
        self.refs = patch('server.live_classroom.teaching_references', return_value=[])
        self.reference_mock=self.refs.start()
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.close()
        self.refs.stop()
        self.folder.cleanup()

    def turn(self, text='請把二十四加十八寫到白板', ident='turn'):
        self.runtime.realtime=True
        self.runtime.rtc_id='synthetic'
        self.runtime.notification('thread/realtime/itemAdded',{'threadId':'primary',
            'item':{'type':'handoff_request','input_transcript':text}})
        self.runtime.notification('turn/started', {'threadId':'primary', 'turn':{'id':ident}})

    async def call(self, name, args, call='call', turn='turn', thread='primary'):
        return await self.runtime.call_tool({'threadId':thread,'turnId':turn,
            'callId':call,'tool':name,'arguments':args})

    def board_args(self, lines=None):
        return {'context_version':self.runtime.context()['context_version'],
            'capture_id':'','lines':lines or ['24 + 18 = 42'],'close':False}

    async def recognize_photo(self, text, capture_meta=None):
        self.observer.turn.return_value = text
        self.runtime.accept_frame('data:image/jpeg;base64,/9j/c3ludGhldGlj', capture_meta=capture_meta)
        await self.runtime.observations.task
        if self.runtime.photo_update_task:
            await self.runtime.photo_update_task
        return self.runtime.context()['photo']

    async def test_completed_photo_pushes_only_index_and_current_tool_reads_bounded_result(self):
        self.runtime.realtime = self.runtime.voice_connected = True
        self.runtime.rtc_id = 'photo-media'
        self.session.notes = '長篇課堂內容'
        observation = '看圖原文 37 + 28。' * 80
        capture_time = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(timespec='seconds')
        photo = await self.recognize_photo(observation, {'captured_at': capture_time})
        self.assertEqual(photo['captured_at'], capture_time)
        self.assertEqual(photo['capture_time_source'], 'device')
        self.assertTrue(photo['recognized_at'])
        self.assertEqual([method for method, _ in self.client.calls], ['thread/inject_items'])
        notice = self.client.calls[0][1]['items'][0]
        self.assertEqual(notice['role'], 'developer')
        notice_text = notice['content'][0]['text']
        self.assertLessEqual(len(notice_text.encode('utf-8')), 512)
        self.assertNotIn('看圖原文', notice_text)
        self.assertNotIn(self.session.notes, notice_text)
        self.assertNotIn('看圖原文', self.runtime.reference_data())
        saved = next(payload for kind, payload in self.records if kind == 'camera_observation')
        self.assertEqual(saved['observation_text'], observation)
        self.assertEqual(saved['recognized_at'], photo['recognized_at'])
        self.assertTrue(any(event['type'] == 'photo_observation_ready' for event in self.events))

        self.turn('請核對照片')
        result = await self.call('get_photo_observation', {'ref': photo['ref']})
        result = json.loads(result['contentItems'][0]['text'])
        self.assertTrue(result['available'])
        self.assertTrue(result['truncated'])
        self.assertEqual(result['observation'], observation[:800])
        self.observer.turn.assert_awaited_once()

        self.runtime.set_camera(False)
        await self.runtime.photo_update_task
        # A retried read with the same call ID still checks the live photo scope.
        result = await self.call('get_photo_observation', {'ref': photo['ref']})
        self.assertFalse(json.loads(result['contentItems'][0]['text'])['available'])
        self.assertEqual(self.runtime.context()['photo'], None)

    async def test_delayed_recognition_cannot_publish_superseded_capture(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def vision(prompt, **kwargs):
            if not entered.is_set():
                entered.set()
                await release.wait()
                return '前一張照片'
            return '最新照片'

        self.observer.turn.side_effect = vision
        self.runtime.realtime = self.runtime.voice_connected = True
        self.runtime.rtc_id = 'photo-media'
        image = 'data:image/jpeg;base64,/9j/c3ludGhldGlj'
        self.runtime.accept_frame(image)
        await asyncio.wait_for(entered.wait(), 1)
        first_id = self.runtime.frame['id']
        self.runtime.accept_frame(image)
        release.set()
        await self.runtime.observations.task
        await self.runtime.photo_update_task
        notices = [params['items'][0]['content'][0]['text'] for method, params in self.client.calls
                   if method == 'thread/inject_items']
        self.assertEqual(len(notices), 1)
        self.assertNotIn(first_id, notices[0])
        self.assertIn(self.runtime.frame['id'], notices[0])

    async def test_background_ticks_leave_time_to_read_a_recognized_photo(self):
        self.observer.turn.return_value = '目前照片的算式'
        image = 'data:image/jpeg;base64,/9j/c3ludGhldGlj'
        self.assertTrue(self.runtime.accept_background_frame(image, None))
        self.assertFalse(self.runtime.accept_background_frame(image, None))
        await self.runtime.observations.task
        current = self.runtime.context()['photo']
        self.assertIsNotNone(current)
        self.assertFalse(self.runtime.accept_background_frame(image, None))
        self.assertEqual(self.runtime.context()['photo'], current)
        self.session.observation['recognized_monotonic'] -= 30
        self.assertTrue(self.runtime.accept_background_frame(image, None))
        self.assertIsNone(self.runtime.context()['photo'])

    async def test_single_photo_is_observed_while_history_update_waits(self):
        self.runtime.realtime = self.runtime.voice_connected = True
        self.runtime.rtc_id = 'photo-media'
        entered = asyncio.Event()
        release = asyncio.Event()
        request = self.client.request

        async def hold_background_sync(method, params, **kwargs):
            if method == 'thread/inject_items' and '最新課堂參考資料' in params['items'][0]['content'][0]['text']:
                entered.set()
                await release.wait()
            return await request(method, params, **kwargs)

        self.client.request = hold_background_sync
        self.observer.turn.return_value = '單張照片上的文字'
        try:
            await self.runtime.apply_interaction({'mode': 'normal', 'reason': '測試課堂方向'})
            await asyncio.wait_for(entered.wait(), 1)
            self.runtime.accept_frame('data:image/jpeg;base64,/9j/c3ludGhldGlj')
            task = self.runtime.observations.task
            self.assertIsNotNone(task)
            await asyncio.wait_for(task, 1)
            self.observer.turn.assert_awaited_once()
            self.assertEqual(self.runtime.current_photo()['text'], '單張照片上的文字')
        finally:
            release.set()

    async def test_photo_background_delivery_failure_keeps_call_and_lookup_available(self):
        self.runtime.realtime = self.runtime.voice_connected = True
        self.runtime.rtc_id = 'photo-media'
        request = self.client.request

        async def drop_notice(method, params, **kwargs):
            if method == 'thread/inject_items':
                raise TimeoutError()
            return await request(method, params, **kwargs)

        with patch.object(self.client, 'request', side_effect=drop_notice), self.assertLogs('tutor.transport', 'ERROR'):
            photo = await self.recognize_photo('看得到算式')
        self.assertTrue(self.runtime.realtime)
        self.assertTrue(self.runtime.voice_connected)
        self.assertEqual(self.runtime.rtc_id, 'photo-media')
        self.assertTrue(photo['ref'])
        self.assertFalse(any(event['type'] == 'native_voice_restart' for event in self.events))

    async def test_new_photo_after_directed_inspection_is_published_independently(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def vision(prompt, **kwargs):
            if not entered.is_set():
                entered.set()
                await release.wait()
                return '指定照片的算式'
            return '新照片的算式'

        self.observer.turn.side_effect = vision
        image = 'data:image/jpeg;base64,/9j/c3ludGhldGlj'
        capture = asyncio.create_task(self.runtime.capture_now())
        await asyncio.sleep(0)
        request_id = self.events[-1]['request_id']
        self.assertTrue(self.runtime.accept_frame(image, request_id=request_id))
        directed = await capture
        self.turn('請核對原照片')
        inspect = asyncio.create_task(self.call('inspect_homework_photo', {'question': '第一題', 'fresh': False}))
        await asyncio.wait_for(entered.wait(), 1)
        self.runtime.accept_frame(image)
        newest = self.runtime.frame['id']
        self.assertNotEqual(directed['id'], newest)
        self.assertFalse(self.runtime.accept_frame(image, request_id=request_id))
        self.assertEqual(self.runtime.frame['id'], newest)
        release.set()
        self.assertTrue((await inspect)['success'])
        await self.runtime.observations.task
        self.assertEqual(self.runtime.context()['photo']['capture_id'], newest)
        self.assertTrue(any(kind == 'camera_observation' and data['capture_id'] == newest for kind, data in self.records))

    async def test_retry_of_withdrawn_photo_inspection_cannot_read_or_recapture(self):
        observed_text = '關閉鏡頭後撤銷這段照片辨識結果'
        await self.recognize_photo(observed_text)
        self.turn('請補拍並核對照片')
        args = {'question': '請核對算式', 'fresh': True}
        capture = AsyncMock(return_value=self.runtime.frame)
        with patch.object(self.runtime, 'capture_now', capture):
            first = await self.call('inspect_homework_photo', args)
            self.assertTrue(first['success'])
            self.assertIn(observed_text, first['contentItems'][0]['text'])
            self.runtime.set_camera(False)
            retry = await self.call('inspect_homework_photo', args)
        self.assertNotIn(observed_text, retry['contentItems'][0]['text'])
        capture.assert_awaited_once()
        self.assertEqual(self.observer.turn.await_count, 2)

    async def test_whiteboard_rejects_a_previous_result_of_the_same_photo(self):
        first = await self.recognize_photo('舊辨識 37 + 28')
        self.turn('請核對算式並寫到白板')
        self.assertTrue((await self.call('get_photo_observation', {'ref': first['ref']}))['success'])
        self.observer.turn.return_value = '重查 37 + 28'
        await self.runtime.observe(self.runtime.frame)
        current = self.runtime.context()['photo']
        self.assertNotEqual(first['ref'], current['ref'])
        args = {**self.board_args(['37 + 28 = 65']), 'capture_id': first['capture_id'], 'photo_ref': first['ref']}
        self.assertFalse((await self.call('update_whiteboard', args, call='stale'))['success'])
        self.assertFalse((await self.call('update_whiteboard', self.board_args(), call='missing'))['success'])
        await self.call('get_photo_observation', {'ref': current['ref']}, call='refresh')
        self.assertTrue((await self.call('update_whiteboard', {**args, 'photo_ref': current['ref']}, call='valid'))['success'])

    async def test_native_transcripts_never_start_another_teacher_or_claim_playback(self):
        self.runtime.realtime = True
        for role, text in [('user','四加八是不是十一？'),('assistant','四加八是十二。')]:
            self.runtime.notification('thread/realtime/transcript/done',
                {'threadId':'primary','role':role,'text':text})
        await asyncio.sleep(.01)
        self.assertEqual(self.client.calls, [])
        self.observer.turn.assert_not_called()
        self.assertEqual(self.session.heard_history,[{'role':'user','text':'四加八是不是十一？'}])
        self.assertEqual(len(self.session.native_history),2)
        self.assertTrue(all(not p['playback_verified'] for k,p in self.records if k=='live_transcript'))
        self.assertNotIn('speech',[e['type'] for e in self.events])

    async def test_whiteboard_is_escaped_and_duplicate_callback_is_idempotent(self):
        self.turn()
        args=self.board_args(['<script>alert(1)</script>','24 + 18 = 42'])
        first=await self.call('update_whiteboard',args)
        second=await self.call('update_whiteboard',args)
        self.assertEqual(first,second)
        self.assertTrue(first['success'])
        boards=[e for e in self.events if e['type']=='board']
        self.assertEqual(len(boards),1)
        self.assertNotIn('<script>',boards[0]['html'])
        wrong=await self.call('update_whiteboard',args,thread='other')
        self.assertFalse(wrong['success'])

    async def test_old_turn_cannot_commit_after_interrupt_or_ui_revision(self):
        self.turn()
        args=self.board_args()
        self.session.plan_revision+=1
        self.assertFalse((await self.call('update_whiteboard',args))['success'])
        self.turn(ident='next')
        args=self.board_args()
        await self.runtime.interrupt()
        self.assertFalse((await self.call('update_whiteboard',args,call='next',turn='next'))['success'])
        self.assertNotIn('board',[e['type'] for e in self.events])

    async def test_native_board_retains_html_support_and_checks_visible_content(self):
        self.turn()
        args={**self.board_args(),'lines':[],'html':'<h1>24 + 18 = 42</h1><button>想一想</button>'}
        self.assertTrue((await self.call('update_whiteboard',args))['success'])
        self.assertEqual(next(e for e in self.events if e['type']=='board')['html'],args['html'])
        self.assertFalse((await self.call('update_whiteboard',{**args,'html':'<p>Codex 內部工具</p>'},call='invalid'))['success'])

    async def test_fabricated_completion_quote_is_rejected_by_existing_lesson_rules(self):
        self.turn('我還沒寫完數學')
        proposal={**empty_lesson(),'tasks':[{'id':'math','title':'數學','status':'done','evidence_quote':'寫完'}]}
        result=await self.call('update_homework_plan',{'context_version':self.runtime.context()['context_version'],
            'lesson':proposal})
        self.assertTrue(result['success'])
        self.assertEqual(self.session.tasks[0]['status'],'todo')
        self.assertFalse(self.session.plan_confirmed)

    async def test_photo_tool_that_finishes_after_direction_change_cannot_commit(self):
        self.turn('請看照片')
        entered=asyncio.Event();done=asyncio.Event()
        async def inspect(*args):
            entered.set();await done.wait();return '37 + 28'
        self.runtime.inspect_photo=inspect
        task=asyncio.create_task(self.call('inspect_homework_photo',{'question':'第一題','fresh':False}))
        await entered.wait()
        self.session.direction_revision+=1
        done.set()
        self.assertFalse((await task)['success'])

    async def test_old_photo_is_not_exposed_and_camera_off_withdraws_it(self):
        self.runtime.frame={'id':'photo','received':time.monotonic(),'camera_epoch':0}
        self.session.observation={'capture_id':'photo','generation':0,'plan_revision':0,
            'captured_received':time.monotonic(),'text':'37 + 28'}
        self.assertEqual(self.runtime.context()['capture_id'],'photo')
        self.session.observation['captured_received']-=121
        self.assertIsNone(self.runtime.context()['photo'])
        self.runtime.set_camera(False)
        self.assertEqual(self.runtime.context()['capture_id'],'')

    async def test_listening_withholds_output_but_preserves_user_evidence(self):
        self.runtime.realtime=True
        await self.runtime.interaction_control('listening')
        self.runtime.notification('thread/realtime/transcript/done',
            {'threadId':'primary','role':'assistant','text':'這段不能當作已聽到。'})
        await asyncio.sleep(.01)
        saved=[p for k,p in self.records if k=='live_transcript'][-1]
        self.assertEqual(saved['delivery'],'withheld')
        self.assertFalse(saved['playback_verified'])
        self.assertTrue(any(e['type']=='native_voice_policy' and e['mode']=='listening' for e in self.events))

    async def test_reconnect_retains_teacher_thread_and_rejects_wrong_sdp(self):
        with self.assertRaises(ValueError):await self.runtime.connect_voice('bad','rtc')
        await self.runtime.connect_voice('v=0\r\nsynthetic','first')
        await self.runtime.connect_voice('v=0\r\nsynthetic','second')
        starts=[p for m,p in self.client.calls if m=='thread/realtime/start']
        self.assertEqual(len(starts),2)
        self.assertTrue(all(p['threadId']=='primary' for p in starts))
        self.assertTrue(all(p['transport']['type']=='webrtc' for p in starts))

    async def test_close_frees_both_models_and_reports_subscription_without_groq_cost(self):
        result=await self.runtime.close()
        self.assertTrue(self.client.closed)
        self.observer.close.assert_awaited_once()
        self.assertEqual(result['voice_engine'],'codex')
        self.assertNotIn('groq_estimate_usd',result)
        self.assertFalse(result['quota_usage_verified'])

    async def test_spoken_listen_and_resume_change_policy_without_another_teacher(self):
        await self.runtime.spoken_control('老師，請先聽我說完，不要回答。')
        self.assertEqual(self.session.interaction_mode,'listening')
        await self.runtime.spoken_control('我說完了，請回答。')
        self.assertEqual(self.session.interaction_mode,'normal')
        self.assertFalse(any(method=='turn/start' for method,_ in self.client.calls))

    async def test_persistence_failure_during_close_still_releases_subscription_models(self):
        def fail(*args):raise OSError('synthetic storage failure')
        self.runtime.persist=fail
        with self.assertRaises(OSError):await self.runtime.close()
        self.assertTrue(self.client.closed)
        self.observer.close.assert_awaited_once()
        self.assertFalse(self.session.active)

    async def test_failed_voice_stop_still_releases_subscription_models(self):
        self.runtime.stop_voice=AsyncMock(side_effect=RuntimeError('synthetic voice stop failure'))
        with self.assertRaisesRegex(RuntimeError,'synthetic voice stop failure'):
            await self.runtime.close()
        self.assertTrue(self.client.closed)
        self.observer.close.assert_awaited_once()
        self.assertFalse(self.session.active)

    async def test_silent_listen_command_works_from_deltas_and_late_final_does_not_override_ui_resume(self):
        self.runtime.realtime=True
        self.runtime.notification('thread/realtime/transcript/delta',{'threadId':'primary','role':'user','delta':'老師請先聽我 說完，不要 回答'})
        await asyncio.sleep(.01)
        self.assertEqual(self.session.interaction_mode,'listening')
        await self.runtime.interaction_control('normal')
        self.runtime.notification('thread/realtime/transcript/done',{'threadId':'primary','role':'user','text':'老師請先聽我說完，不要回答'})
        await asyncio.sleep(.01)
        self.assertEqual(self.session.interaction_mode,'normal')

    async def test_typed_reply_is_shown_and_forwarded_to_native_audio_without_a_second_model(self):
        await self.runtime.submit_text('二十四加十八？')
        self.assertEqual(self.runtime.turns['typed'].request,'二十四加十八？')
        self.runtime.notification('turn/started',{'threadId':'primary','turn':{'id':'typed'}})
        self.runtime.notification('item/completed',{'threadId':'primary','turnId':'typed',
            'item':{'type':'agentMessage','phase':'final','text':'二十四加十八等於四十二。'}})
        self.runtime.notification('turn/completed',{'threadId':'primary','turn':{'id':'typed','status':'completed'}})
        await asyncio.sleep(.01)
        self.assertTrue(any(e['type']=='transcript' and e.get('delivery')=='text_only' for e in self.events))
        self.assertFalse(any(m=='thread/realtime/appendSpeech' for m,p in self.client.calls))
        await self.runtime.connect_voice('v=0 synthetic','new-rtc')
        await self.runtime.voice_ready('retired-rtc')
        self.assertFalse(any(m=='thread/realtime/appendSpeech' for m,p in self.client.calls))
        await self.runtime.voice_ready('new-rtc')
        self.assertTrue(any(m=='thread/realtime/appendSpeech' and p['threadId']=='primary' for m,p in self.client.calls))
        self.assertFalse(any(x['role']=='assistant' for x in self.session.heard_history))

    async def test_early_text_callbacks_wait_for_exact_turn_id_and_survive_rtc_rebuild(self):
        original=self.client.request
        callback=None
        async def request(method,params,**kwargs):
            nonlocal callback
            if method=='turn/start':
                self.runtime.notification('turn/started',{'threadId':'primary','turn':{'id':'retired-native'}})
                callback=asyncio.create_task(self.call('get_lesson_context',{},turn='typed'))
                self.runtime.notification('item/completed',{'threadId':'primary','turnId':'typed',
                    'item':{'type':'agentMessage','phase':'final','text':'答案是四十二。'}})
                self.runtime.notification('turn/completed',{'threadId':'primary','turn':{'id':'typed','status':'completed'}})
                await asyncio.sleep(.01)
                self.assertFalse(callback.done())
            return await original(method,params,**kwargs)
        self.client.request=request
        await self.runtime.submit_text('二十四加十八？')
        # A connection changes before queued backend completions are consumed.
        self.runtime.rtc_id='rebuilt';self.runtime.realtime=True
        result=await callback
        self.assertTrue(result['success'])
        await asyncio.sleep(.01)
        self.assertNotIn('retired-native',self.runtime.turns)
        self.assertTrue(any(e.get('text')=='答案是四十二。' for e in self.events))
        await self.runtime.voice_ready('rebuilt')
        self.assertTrue(any(m=='thread/realtime/appendSpeech' for m,p in self.client.calls))

    async def test_pending_typed_speech_is_withdrawn_after_lesson_changes(self):
        await self.runtime.submit_text('二十四加十八？')
        self.runtime.notification('item/completed',{'threadId':'primary','turnId':'typed',
            'item':{'type':'agentMessage','phase':'final','text':'答案是四十二。'}})
        self.runtime.notification('turn/completed',{'threadId':'primary','turn':{'id':'typed','status':'completed'}})
        await asyncio.sleep(.01)
        self.session.plan_revision+=1
        await self.runtime.connect_voice('v=0 synthetic','new-rtc')
        await self.runtime.voice_ready('new-rtc')
        self.assertFalse(any(m=='thread/realtime/appendSpeech' for m,p in self.client.calls))

    async def test_learning_help_requires_current_quote_and_identified_student(self):
        self.reference_mock.return_value=[{'code':'n-I-1','subject':'數學','summary':'整數加法','page':1}]
        self.session.input_speaker='unknown'
        self.turn('我不會二十四加十八')
        args={'context_version':self.runtime.context()['context_version'],'code':'n-I-1',
            'quote':'我不會二十四加十八','reason':'本次明確求助'}
        self.assertFalse((await self.call('record_learning_help',{**args,'quote':'乘法不會'},call='fabricated'))['success'])
        self.assertTrue((await self.call('record_learning_help',args,call='unknown'))['success'])
        self.assertTrue(any(k=='learning_candidate' for k,p in self.records))
        self.assertFalse(any(k=='learning_evidence' for k,p in self.records))
        self.session.input_speaker='student'
        self.turn('我不會二十四加十八',ident='student-turn')
        self.assertTrue((await self.call('record_learning_help',args,call='student',turn='student-turn'))['success'])
        evidence=next(p for k,p in self.records if k=='learning_evidence')
        self.assertEqual(evidence['source_id'],'student-turn')
        self.assertEqual(evidence['speaker'],'student')

    async def test_voice_switch_restarts_media_on_same_teacher_and_invalidates_old_work(self):
        self.runtime.voice='arbor'
        await self.runtime.connect_voice('v=0 synthetic','old-media')
        self.assertEqual(next(p for m,p in self.client.calls if m=='thread/realtime/start')['voice'],'arbor')
        self.turn(ident='old-turn');args=self.board_args()
        generation=self.session.generation
        await self.runtime.select_voice('cove')
        self.assertEqual(self.runtime.voice,'cove')
        self.assertGreater(self.session.generation,generation)
        self.assertFalse(self.runtime.realtime)
        self.assertFalse((await self.call('update_whiteboard',args,turn='old-turn'))['success'])
        await self.runtime.connect_voice('v=0 synthetic','new-media')
        start=[p for m,p in self.client.calls if m=='thread/realtime/start'][-1]
        self.assertEqual((start['threadId'],start['voice']),('primary','cove'))
        self.assertIn(('native_voice_selection',{'voice':'cove'}),self.records)

    async def test_unsupported_voice_is_rejected_before_interrupt_and_default_omits_override(self):
        await self.runtime.connect_voice('v=0 synthetic','old-media')
        start=[p for m,p in self.client.calls if m=='thread/realtime/start'][-1]
        self.assertNotIn('voice',start)
        generation=self.session.generation
        for voice in ('marin','invalid',None,{}):
            with self.assertRaises(ValueError):await self.runtime.select_voice(voice)
        self.assertTrue(self.runtime.realtime)
        self.assertEqual(self.session.generation,generation)
        original=self.client.request
        self.client.request=AsyncMock(return_value={'voices':{'v1':['cove']}})
        with self.assertRaises(ValueError):await self.runtime.select_voice('arbor')
        self.assertEqual(self.runtime.voice,'')
        self.client.request=original

    async def test_voice_switch_preserves_listening_and_pending_speech_stays_withdrawn(self):
        await self.runtime.interaction_control('listening')
        self.runtime.pending_reply=('過期回答',self.session.generation,self.session.direction_revision,self.session.plan_revision)
        await self.runtime.select_voice('arbor')
        await self.runtime.connect_voice('v=0 synthetic','new-media')
        await self.runtime.voice_ready('new-media')
        self.assertEqual(self.session.interaction_mode,'listening')
        self.assertFalse(any(m=='thread/realtime/appendSpeech' for m,p in self.client.calls))

    async def test_authenticated_bridge_changes_voice_and_keeps_invalid_choice_recoverable(self):
        from .transport import RuntimeBridge
        runtime=self.runtime
        runtime.select_voice=AsyncMock(wraps=runtime.select_voice)
        class Socket:
            step=0
            events=[]
            async def accept(self):pass
            async def receive_json(self):return {'type':'authenticate','token':'synthetic-token'}
            async def send_json(self,event):self.events.append(event)
            async def receive_text(self):
                self.step+=1
                await asyncio.sleep(.01)
                event=({'type':'native_voice_select','voice':'marin'} if self.step==1 else
                       {'type':'native_voice_select','voice':'arbor'} if self.step==2 else {'type':'close'})
                return json.dumps({'session_id':runtime.session.id,**event})
            async def close(self,code):pass
        socket=Socket()
        await RuntimeBridge(runtime).serve(socket,'synthetic-token')
        self.assertEqual(runtime.voice,'arbor')
        self.assertEqual(runtime.select_voice.await_count,2)
        self.assertEqual(socket.events[0]['codex_voice'],'')
        self.assertTrue(any(e['type']=='native_voice_selected' and e['voice']=='arbor' for e in socket.events))
        self.assertEqual(socket.events[-1]['type'],'closed')


class ProvisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_key_native_start_resume_and_invalid_route(self):
        from .test_support import SeededStore
        from fastapi import HTTPException
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=SeededStore(root)
            with patch.dict(os.environ,{'TUTOR_DATA_DIR':directory,'TUTOR_CORE_TOKEN':'synthetic'}):
                from . import app
            clients=[];observers=[]
            def client(*args):
                value=FakeClient();clients.append(value);return value
            class Observer:
                vision_only=True;collaboration_tools='none'
                def __init__(self,workspace,*args):
                    self.workspace=workspace;self.closed=False;observers.append(self)
                async def ensure_ready(self):pass
                async def close(self):self.closed=True
            with patch.object(app,'ROOT',root),patch.object(app,'store',store),patch.object(app,'active',None),\
                patch.object(app,'bridge',None),\
                patch.object(app,'runtime_factory',app.create_runtime),\
                patch('server.codex_voice.SubscriptionCodex',Observer),\
                patch('server.native_basic.CodexVoiceClient',side_effect=client):
                for bad in ({'codex_voice':'marin'},):
                    with self.assertRaises(HTTPException) as error:
                        await app.start(app.Start(student_id='student-2',mode='basic',basic_voice='codex',**bad))
                    self.assertEqual(error.exception.status_code,422)
                started=await app.start(app.Start(student_id='student-2',mode='basic',basic_voice='codex',codex_voice='arbor'))
                self.assertTrue(started['online'])
                self.assertEqual(started['basic_voice'],'codex')
                runtime=app.bridge.runtime
                self.assertIsInstance(runtime, ClassroomRuntime)
                self.assertFalse(hasattr(runtime, 'speech'))
                self.assertEqual(runtime.voice,'arbor')
                await runtime.select_voice('cove')
                runtime.session.native_history=[{'role':'assistant','text':'先分開十位和個位。','playback_verified':False}]
                runtime.session.input_speaker='onsite_teacher'
                await app.bridge.close()
                store.finish('student-2',started['id'],'interrupted')
                app.active=None;app.bridge=None
                resumed=await app.start(app.Start(student_id='student-2',resume_session_id=started['id']))
                self.assertEqual(resumed['id'],started['id'])
                self.assertEqual(resumed['basic_voice'],'codex')
                self.assertEqual(resumed['codex_voice'],'cove')
                self.assertEqual(app.bridge.runtime.voice,'cove')
                self.assertEqual(app.bridge.runtime.session.native_history,runtime.session.native_history)
                self.assertEqual(app.bridge.runtime.session.input_speaker,'onsite_teacher')
                self.assertIsNone(app.bridge.runtime.session.observation)
                await app.bridge.close()
                store.finish('student-2',started['id'])
                self.assertTrue(all(x.closed for x in clients+observers))
