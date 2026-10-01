import asyncio
import json
import unittest
from .live import GPTLive, GeminiLive, UsageLedger
from .tutor import TutorSession, TutorEngine

class FakeSocket:
    def __init__(self):self.sent=[];self.closed=False
    async def send_json(self,payload):self.sent.append(payload)
    async def close(self):self.closed=True

class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_responses_voice_prompt_lists_registered_tools_and_initial_vision_handoff(self):
        provider=GPTLive('test',teacher_name='測試老師',instructions='{"年級":4}',
            delegation={'type':'responses','model':'gpt-6.1-sol','effort':'low'},backend_instructions='教學規則')
        session=provider.setup()['session']
        prompt=session['instructions']
        self.assertIn('你叫測試老師',prompt)
        self.assertTrue(prompt.endswith('{"年級":4}'))
        for tool in session['delegation']['responses']['tools']:
            self.assertIn(tool['name']+'(',prompt)
        self.assertIn('開始陪讀時主動委派一次',prompt)
        self.assertIn('背景觀察維持安靜',prompt)
        self.assertEqual(session['delegation']['responses']['model'],'gpt-6.1-sol')
    async def test_client_voice_prompt_explains_available_capabilities_without_fake_function_tools(self):
        for client in ('codex','agy'):
            provider=GPTLive('test',delegation={'type':'client','provider':client})
            session=provider.setup()['session'];prompt=session['instructions']
            self.assertEqual(session['delegation'],{'type':'client'})
            self.assertIn('看作業：',prompt)
            self.assertIn('重新拍照並辨識',prompt)
            self.assertIn('白板：',prompt)
            self.assertIn('不需要先搜尋環境',prompt)
            self.assertNotIn('inspect_photo(',prompt)
            self.assertIn('不逐張朗讀',prompt)
    async def test_gpt_background_context_still_uses_thinking_event(self):
        provider=GPTLive('test');provider.ws=FakeSocket();provider.ready=True
        await provider.append('背景作業觀察，無須朗讀或打斷：合成題目 7＋5。')
        self.assertEqual(provider.ws.sent[0]['type'],'session.thinking.append')
        self.assertIsNone(provider.ws.sent[0]['delegation_id'])
    async def test_gpt_ready_mute_close_and_cost(self):
        p=GPTLive('test');p.ws=FakeSocket()
        await p.send_audio(b'\0\0');self.assertEqual(p.ws.sent,[])
        p.normalize({'type':'session.started','session':{'id':'voice-a'}})
        await p.send_audio(b'\0\0');await p.mute(True)
        p.normalize({'type':'session.usage.updated','usage':{'seconds':30}})
        p.normalize({'type':'session.closed','usage':{'seconds':60}})
        self.assertAlmostEqual(p.ledger.report()['gpt_voice_estimate'],.05)
        self.assertTrue(p.ledger.report()['gpt_final'])
    async def test_gemini_end_of_stream_not_turn_interrupt(self):
        p=GeminiLive('test');p.ws=FakeSocket();p.normalize({'setupComplete':{}})
        await p.mute(True);await p.append('安靜陪伴')
        self.assertEqual(p.ws.sent[0],{'realtimeInput':{'audioStreamEnd':True}})
        self.assertFalse(p.ws.sent[1]['clientContent']['turnComplete'])
        self.assertNotIn('proactivity',p.setup()['setup'])
        self.assertIsNone(p.ledger.report()['gemini_estimate'])
    async def test_late_tutor_reply_does_not_cross_interruption(self):
        session=TutorSession('test',2);ready=asyncio.Event();release=asyncio.Event()
        class Provider:
            async def turn(self,*args,**kwargs):
                ready.set();await release.wait();return json.dumps({'quiet':False,'speech':'hint','board_html':'','close_board':False,'need_capture':False,'evidence':[]})
        task=asyncio.create_task(TutorEngine(Provider()).respond(session,'help'))
        await ready.wait();session.interrupt({'fully_heard':'previous'});release.set()
        self.assertIsNone(await task)
    async def test_observation_never_speaks_or_changes_board(self):
        class Provider:
            async def turn(self,*args,**kwargs):
                from .lesson import empty_lesson
                return json.dumps({'quiet':False,'speech':'interrupt','board_html':'<h1>answer</h1>',
                    'close_board':True,'need_capture':False,'evidence':['a visible number'],
                    'lesson':empty_lesson()})
        result=await TutorEngine(Provider()).respond(TutorSession('test',4),'',observation=True)
        self.assertEqual(result['speech'],'');self.assertEqual(result['board_html'],'');self.assertFalse(result['close_board'])

if __name__=='__main__':unittest.main()
