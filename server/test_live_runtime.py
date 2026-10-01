import asyncio
import unittest
import time
import base64
import tempfile
from pathlib import Path
from unittest.mock import patch
from .live_runtime import LiveRuntime,chunks
from .live import UsageLedger
from .test_basic import Teacher
from .tutor import TutorSession

class Provider:
    def __init__(self):
        self.ready=True;self.ledger=UsageLedger();self.sent=[];self.closed=False;self.runtime=None
    async def send_audio(self,data):self.sent.append(('audio',data))
    async def mute(self,value):self.sent.append(('mute',value))
    async def append(self,*args,**kwargs):self.sent.append(('append',args,kwargs))
    async def send_image(self,data):self.sent.append(('image',data))
    async def tool_result(self,call,result):self.sent.append(('tool',call,result))
    async def response_tool_output(self,call_id,result):self.sent.append(('response_tool',call_id,result))
    async def response_continue(self):self.sent.append(('response_continue',))
    async def response_user_item(self,text):self.sent.append(('response_user_item',text))
    async def response_text(self,text):self.sent.append(('response_text',text))
    async def request_close(self):
        self.ledger.gpt_snapshot('connection',60,True);self.runtime.final_event.set()
    async def release(self):self.closed=True;self.ready=False

class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_lossless_png_survives_validation_and_audit_unchanged(self):
        from .audit import SessionAudit
        runtime = self.make()
        runtime.observations.push = lambda frame: None
        # The full-resolution PNG must survive the old JPEG-sized 3 MB ceiling.
        data = b'\x89PNG\r\n\x1a\n' + bytes(4_000_000)
        image = 'data:image/png;base64,' + base64.b64encode(data).decode()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime.audit = SessionAudit(root/'audit', 'png-probe', 'gpt', 'openai', root)
            runtime.accept_frame(image)
            self.assertEqual(runtime.frame['image'], image)
            await asyncio.gather(*runtime.photo_tasks)
            files = list((runtime.audit.root/'photos').glob('*.png'))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), data)
            with self.assertRaisesRegex(ValueError, '格式'):
                runtime.accept_frame('data:image/png;base64,' + base64.b64encode(b'\xff\xd8\xffwrong').decode())
            await runtime.close()

    def make(self,mode='gpt'):
        self.events=[];self.saved=[]
        async def emit(event):self.events.append(event)
        runtime=LiveRuntime(TutorSession('test',2),Teacher(),Teacher(),mode,'test',emit,lambda *args:self.saved.append(args))
        runtime.provider=Provider();runtime.provider.runtime=runtime
        return runtime
    async def test_final_usage_retained_on_repeated_close(self):
        runtime=self.make()
        first=await runtime.close();second=await runtime.close()
        self.assertEqual(first,second);self.assertTrue(first['gpt_final'])
        self.assertAlmostEqual(first['gpt_voice_estimate'],.05)
        self.assertTrue(runtime.provider.closed)
    async def test_mute_discards_queued_audio_even_after_unmute(self):
        runtime=self.make();runtime.accept_audio(b'\0\0')
        runtime.set_mic(False);runtime.set_mic(True)
        runtime.writer=asyncio.create_task(runtime.write_provider())
        await asyncio.sleep(.01)
        self.assertFalse(any(e[0]=='audio' for e in runtime.provider.sent))
        self.assertEqual(runtime.provider.sent,[('mute',True),('mute',False)])
        await runtime.close()
    async def test_delegation_uses_transcript_and_preserves_id(self):
        runtime=self.make();await runtime.gpt_event({'type':'session.input_transcript.delta','delta':'七加五？'})
        await runtime.gpt_event({'type':'session.delegation.created','delegation':{'target':'client','id':'delegation-7'}})
        await asyncio.gather(*list(runtime.tools.values()))
        reply=next(e for e in runtime.provider.sent if e[0]=='append')
        self.assertEqual(reply[1][1],'delegation-7')
        self.assertTrue(any(e['type']=='board' for e in self.events))
        self.assertFalse(self.saved[0][1]['playback_verified'])
        await runtime.close()

    async def test_client_delegation_uses_latest_typed_or_spoken_request(self):
        runtime=self.make()
        requests=[]
        async def decision(request):
            requests.append(request)
            return {'speech':'下一步','board_html':'','close_board':False}
        runtime.decision=decision
        runtime.accept_text('打字問題')
        await asyncio.gather(*runtime.voice_tasks)
        await runtime.gpt_event({'type':'session.delegation.created',
                                 'delegation':{'target':'client','id':'typed'}})
        await asyncio.gather(*runtime.tools.values())
        await runtime.gpt_event({'type':'session.input_transcript.delta','delta':'口說問題'})
        await runtime.gpt_event({'type':'session.delegation.created',
                                 'delegation':{'target':'client','id':'spoken'}})
        await asyncio.gather(*runtime.tools.values())
        self.assertEqual(requests[0],'打字問題')
        self.assertIn('照片觀察',requests[1])
        self.assertTrue(requests[1].endswith('打字問題'))
        self.assertEqual(requests[2],'口說問題')
        self.assertEqual(runtime.session.heard_history[-1]['text'],'口說問題')
        await runtime.close()

    async def test_client_opening_delegation_requests_photo_and_scope_without_student_instruction(self):
        runtime=self.make();requests=[]
        async def decision(request):
            requests.append(request)
            return {'speech':'請確認作業範圍','board_html':'','close_board':False}
        runtime.decision=decision
        await runtime.gpt_event({'type':'session.delegation.created',
            'delegation':{'target':'client','id':'opening-photo'}})
        await asyncio.gather(*runtime.tools.values())
        self.assertEqual(len(requests),1)
        self.assertIn('照片觀察',requests[0])
        self.assertIn('作業範圍',requests[0])
        self.assertIn('要求拍攝新照片',requests[0])
        await runtime.gpt_event({'type':'session.delegation.created',
            'delegation':{'target':'client','id':'follow-up'}})
        await asyncio.gather(*runtime.tools.values())
        self.assertNotIn('開始陪讀：',requests[1])
        reply=next(item for item in runtime.provider.sent if item[0]=='append')
        self.assertEqual(reply[1][1],'opening-photo')
        await runtime.close()

    async def test_responses_function_calls_return_before_continuation(self):
        self.events=[];self.saved=[]
        config={'version':1,'delegation':{'type':'responses','model':'gpt-6.1-sol','effort':'low'},
                'vision':{'provider':'openai','model':'gpt-6-luna','effort':'max'}}
        async def emit(event):self.events.append(event)
        runtime=LiveRuntime(TutorSession('test',2),Teacher(),Teacher(),'gpt','test',emit,
                            lambda *args:self.saved.append(args),gpt_config=config)
        runtime.provider=Provider();runtime.provider.runtime=runtime
        await runtime.gpt_event({'type':'response.event','delegation_id':'d1','event':{
            'type':'response.output_item.done','item':{'type':'function_call',
                'call_id':'c1','name':'get_lesson_context','arguments':'{}'}}})
        await runtime.gpt_event({'type':'response.event','delegation_id':'d1','event':{
            'type':'response.completed','response':{'id':'r1','usage':{'input_tokens':8}}}})
        await asyncio.gather(*runtime.tools.values())
        self.assertEqual(runtime.provider.sent[0][0:2],('response_tool','c1'))
        self.assertEqual(runtime.provider.sent[1],('response_continue',))
        self.assertEqual(runtime.response_usage[0]['model'],'gpt-6.1-sol')
        await runtime.close()

    async def test_responses_typed_input_waits_for_pending_tool_output(self):
        config={'version':1,'delegation':{'type':'responses','model':'gpt-6.1-sol','effort':'low'},
                'vision':{'provider':'openai','model':'gpt-6-luna','effort':'max'}}
        runtime=LiveRuntime(TutorSession('test',2),Teacher(),Teacher(),'gpt','test',
                            lambda event:asyncio.sleep(0),lambda *args:None,gpt_config=config)
        runtime.provider=Provider();runtime.provider.runtime=runtime
        await runtime.gpt_event({'type':'response.event','delegation_id':'d1','event':{
            'type':'response.output_item.done','item':{'type':'function_call',
                'call_id':'c1','name':'get_lesson_context','arguments':'{}'}}})
        await runtime.gpt_text('打字補充')
        self.assertEqual(runtime.provider.sent,[])
        await runtime.gpt_event({'type':'response.event','delegation_id':'d1','event':{
            'type':'response.completed','response':{'id':'r1'}}})
        await asyncio.gather(*runtime.tools.values())
        self.assertEqual([item[0] for item in runtime.provider.sent],
                         ['response_tool','response_user_item','response_continue'])
        await runtime.close()

    async def test_responses_context_includes_history_and_curriculum_source(self):
        config={'version':1,'delegation':{'type':'responses','model':'gpt-6.1-sol','effort':'low'},
                'vision':{'provider':'openai','model':'gpt-6-luna','effort':'max'}}
        runtime=LiveRuntime(TutorSession('test',2),Teacher(),Teacher(),'gpt','test',
                            lambda event:asyncio.sleep(0),lambda *args:None,gpt_config=config)
        runtime.provider=Provider();runtime.provider.runtime=runtime
        runtime.latest_user_request='七加五怎麼算？'
        runtime.session.memory=[{'session_id':'previous','student_questions':['加法']}]
        runtime.session.long_term_memory=[{'code':'N-1-3','parent_note':'先用積木'}]
        reference={'code':'N-1-3','subject':'數學','kind':'學習內容',
                   'grade_min':1,'grade_max':1,'summary':'基本加法','source_title':'108 數學課綱',
                   'page':22,'source_url':'https://example.test/curriculum','quality':'auto',
                   'prerequisite':True}
        with patch('server.live_runtime.teaching_references',return_value=[reference]) as lookup,\
             patch('server.live_runtime.catalog_status',return_value={'status':'indexed'}):
            context=await runtime.responses_tool('get_lesson_context',{})
            curriculum=await runtime.responses_tool('get_curriculum_references',{'topic':'加法'})
        self.assertEqual(context['prior_lesson_context'][0]['session_id'],'previous')
        self.assertEqual(context['learning_notes'][0]['parent_note'],'先用積木')
        self.assertEqual(context['curriculum']['references'][0]['source_page'],22)
        self.assertEqual(curriculum['references'][0]['code'],'N-1-3')
        self.assertEqual(curriculum['review'],'automatic_unreviewed')
        self.assertEqual(lookup.call_args.args[0],2)
        await runtime.close()

    async def test_responses_board_call_from_interrupted_turn_is_cancelled(self):
        self.events=[];self.saved=[]
        config={'version':1,'delegation':{'type':'responses','model':'gpt-6.1-sol','effort':'low'},
                'vision':{'provider':'openai','model':'gpt-6-luna','effort':'max'}}
        async def emit(event):self.events.append(event)
        runtime=LiveRuntime(TutorSession('test',2),Teacher(),Teacher(),'gpt','test',emit,
                            lambda *args:self.saved.append(args),gpt_config=config)
        runtime.provider=Provider();runtime.provider.runtime=runtime
        await runtime.gpt_event({'type':'session.delegation.created',
                                 'delegation':{'target':'responses','id':'old'}})
        await runtime.gpt_event({'type':'response.event','delegation_id':'old','event':{
            'type':'response.output_item.done','item':{'type':'function_call','call_id':'board-1',
                'name':'set_board','arguments':'{"html":"<h1>old</h1>"}'}}})
        runtime.session.interrupt({})
        await runtime.gpt_event({'type':'response.event','delegation_id':'old','event':{
            'type':'response.completed','response':{'id':'old-response'}}})
        await asyncio.gather(*runtime.tools.values())
        self.assertFalse(any(event['type']=='board' for event in self.events))
        self.assertEqual(runtime.provider.sent[0][2],{'cancelled':True})
        await runtime.close()

    async def test_selected_client_teacher_does_not_receive_raw_photo(self):
        self.events=[];self.saved=[]
        config={'version':1,'delegation':{'type':'client','provider':'codex','model':'gpt-6.1-sol','effort':'low'},
                'vision':{'provider':'openai','model':'gpt-6-luna','effort':'max'}}
        async def emit(event):self.events.append(event)
        runtime=LiveRuntime(TutorSession('test',2),Teacher(),Teacher(),'gpt','test',emit,
                            lambda *args:self.saved.append(args),gpt_config=config)
        runtime.provider=Provider();runtime.provider.runtime=runtime
        seen=[]
        async def respond(*args,**kwargs):
            seen.append(kwargs.get('image_url'))
            return {'quiet':False,'speech':'先看題目','board_html':'','close_board':False,
                    'need_capture':False,'evidence':[]}
        runtime.teacher.respond=respond
        runtime.frame={'id':'p1','image':'data:image/jpeg;base64,/9j/',
                       'camera_epoch':runtime.camera_epoch,'received':time.monotonic()}
        await runtime.decision('這題怎麼做')
        self.assertEqual(seen,[None])
        await runtime.close()
    async def test_transcript_deltas_update_one_turn_in_context(self):
        runtime=self.make()
        await runtime.transcript('user','七加')
        await runtime.transcript('user','五？')
        first=[event for event in self.events if event['type']=='transcript']
        self.assertEqual(len(runtime.session.heard_history),1)
        self.assertEqual(runtime.session.heard_history[0]['text'],'七加五？')
        self.assertEqual(first[0]['transcript_id'],first[1]['transcript_id'])
        self.assertEqual(first[1]['text'],'七加五？')
        await runtime.transcript('assistant','先想')
        self.assertNotEqual(first[1]['transcript_id'],self.events[-1]['transcript_id'])
        self.assertEqual(len(runtime.session.heard_history),2)
        await runtime.close()
    async def test_gemini_capture_tool_gets_fresh_frame(self):
        runtime=self.make('gemini')
        task=asyncio.create_task(runtime.gemini_tool({'id':'photo','name':'capture_now'}))
        await asyncio.sleep(0)
        request=self.events[-1]['request_id']
        runtime.accept_frame('data:image/jpeg;base64,/9j/',request)
        await task
        self.assertIn(('image',b'\xff\xd8\xff'),runtime.provider.sent)
        self.assertEqual(runtime.provider.sent[-1][2],{'captured':True})
        await runtime.close()

    async def test_invalid_live_photo_does_not_replace_current_frame(self):
        runtime=self.make('gemini')
        runtime.accept_frame('data:image/jpeg;base64,/9j/')
        current=runtime.frame
        with self.assertRaisesRegex(ValueError,'照片編碼無效'):
            runtime.accept_frame('data:image/jpeg;base64,%%%')
        with self.assertRaisesRegex(ValueError,'照片格式無效'):
            runtime.accept_frame('data:image/jpeg;base64,AA==')
        self.assertIs(runtime.frame,current)
        await runtime.close()

    async def test_gemini_capture_does_not_send_frame_after_camera_off(self):
        runtime=self.make('gemini')
        task=asyncio.create_task(runtime.gemini_tool({'id':'photo','name':'capture_now'}))
        await asyncio.sleep(0)
        request=self.events[-1]['request_id']
        runtime.accept_frame('data:image/jpeg;base64,/9j/',request)
        runtime.set_camera(False)
        await task
        self.assertFalse(any(event[0]=='image' for event in runtime.provider.sent))
        self.assertEqual(runtime.provider.sent[-1][2],{'captured':False})
        await runtime.close()

    async def test_gemini_capture_does_not_send_after_interruption(self):
        runtime=self.make('gemini')
        async def respond(*args,**kwargs):
            return {'need_capture':True,'board_html':'','close_board':False,
                    'speech':'','evidence':[]}
        async def capture():
            runtime.session.interrupt({})
            return {'image':'data:image/jpeg;base64,AA==','camera_epoch':runtime.camera_epoch}
        runtime.teacher.respond=respond
        runtime.capture_now=capture
        self.assertIsNone(await runtime.decision('請看新照片'))
        self.assertFalse(any(event[0]=='image' for event in runtime.provider.sent))
        await runtime.close()

    async def test_gemini_uses_native_image_without_cli_vision(self):
        runtime=self.make('gemini')
        async def unexpected(*args,**kwargs):self.fail('Gemini photo reached CLI observer')
        runtime.observer.respond=unexpected
        frame={'id':'native','image':'data:image/jpeg;base64,AA==',
               'camera_epoch':0,'received':time.monotonic()}
        runtime.frame=frame
        await runtime.observe(frame)
        self.assertIn(('image',b'\0'),runtime.provider.sent)
        seen=[]
        async def teach(*args,**kwargs):
            seen.append(kwargs.get('image_url'))
            return {'need_capture':False,'board_html':'','close_board':False,
                    'speech':'一小步提示','evidence':[]}
        runtime.teacher.respond=teach
        await runtime.decision('請提示')
        self.assertEqual(seen,[None])
        await runtime.close()
    async def test_cancelled_gemini_tool_cannot_change_board(self):
        runtime=self.make('gemini');entered=asyncio.Event();release=asyncio.Event()
        async def delayed(*args,**kwargs):entered.set();await release.wait()
        runtime.teacher.respond=delayed
        await runtime.gemini_event({'toolCall':{'functionCalls':[{'id':'task','name':'tutor_help','args':{'request':'help'}}]}})
        await entered.wait()
        tasks=list(runtime.tools.values())
        await runtime.gemini_event({'toolCallCancellation':{'ids':['task']}})
        await asyncio.gather(*tasks,return_exceptions=True)
        self.assertFalse(any(e['type']=='board' for e in self.events))
        await runtime.close()
    async def test_unknown_cost_is_not_zero_and_chunks_are_utf8_bounded(self):
        self.assertIsNone(UsageLedger().report()['gpt_voice_estimate'])
        text='伴讀老師🙂'*200
        parts=list(chunks(text))
        self.assertEqual(''.join(parts),text)
        self.assertTrue(all(len(p.encode('utf-8'))<=480 for p in parts))
    async def test_live_help_records_actual_transcript_only_once(self):
        runtime=self.make()
        await runtime.transcript('user','加減法怎麼算？')
        async def decide(*args,**kwargs):
            return {'need_capture':False,'board_html':'','close_board':False,
                'curriculum_candidates':[{'code':'N-2-2','subject':'數學','label':'加減算式',
                    'page':23,'source':'數學課綱'}]}
        runtime.teacher.respond=decide
        await runtime.decision('由模型改寫的請求')
        await runtime.decision('由模型改寫的請求')
        recorded=[payload for kind,payload in self.saved if kind=='learning_evidence']
        self.assertEqual(len(recorded),1)
        self.assertEqual(recorded[0]['quote'],'加減法怎麼算？')
        self.assertEqual(recorded[0]['origin'],'provider_transcript')
        await runtime.close()
    async def test_live_camera_progress_uses_same_student_memory(self):
        runtime=self.make()
        calls=0
        async def observe(*args,**kwargs):
            nonlocal calls
            calls+=1
            return {'evidence':['7＋5 = ?'] if calls==1 else ['7＋5 寫出下一步 10＋2'],
                'lesson':{'signal':'none' if calls==1 else 'completed_step','focus':'7＋5','confidence':'high'}}
        runtime.observer.respond=observe
        ref={'code':'N-1-3','subject':'數學','summary':'基本加減法：操作活動','page':22}
        with patch('server.basic.teaching_references',return_value=[ref]):
            for index in range(3):
                await runtime.observe({'id':str(index),'image':'data:image/jpeg;base64,AA==',
                    'camera_epoch':0,'received':time.monotonic()})
                if index==1:runtime.signal_since-=11
        recorded=[payload for kind,payload in self.saved if kind=='learning_evidence']
        self.assertEqual(len(recorded),1)
        self.assertEqual(recorded[0]['kind'],'observed_step')
        await runtime.close()

if __name__=='__main__':unittest.main()
