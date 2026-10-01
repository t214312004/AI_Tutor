import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
import json

import httpx

from .gpt_config import api_models, validate_route
from .live import GPTLive
from .providers import CodexClient, AgyClient
from .openai_vision import OpenAIResponseVision


class GptConfigTests(unittest.TestCase):
    def setUp(self):
        self.options = {
            'openai': {'models': api_models(), 'error': None},
            'codex': {'models': [{'id': 'gpt-6.1-sol', 'label': 'Sol',
                                  'efforts': ['low', 'medium'], 'image': True}], 'error': None},
            'agy': {'models': [{'id': 'gemini-flash-high', 'label': 'Flash High',
                                'efforts': ['high'], 'image': True}], 'error': None},
        }

    def test_separate_responses_teaching_and_vision(self):
        config = {'version': 1,
                  'delegation': {'type': 'responses', 'model': 'gpt-6.1-sol', 'effort': 'low'},
                  'vision': {'provider': 'openai', 'model': 'gpt-6-luna', 'effort': 'max'}}
        self.assertEqual(validate_route(config, self.options)['vision']['effort'], 'max')
        setup = GPTLive('key', delegation=config['delegation']).setup()['session']['delegation']
        self.assertIn('先前學習紀錄',GPTLive('key',delegation=config['delegation']).setup()['session']['instructions'])
        self.assertEqual(setup['responses']['model'], 'gpt-6.1-sol')
        self.assertEqual(setup['responses']['reasoning']['effort'], 'low')
        self.assertIn('inspect_photo', [tool['name'] for tool in setup['responses']['tools']])
        self.assertIn('get_curriculum_references',
                      [tool['name'] for tool in setup['responses']['tools']])

    def test_client_route_and_unsupported_effort(self):
        config = {'version': 1,
                  'delegation': {'type': 'client', 'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'low'},
                  'vision': {'provider': 'agy', 'model': 'gemini-flash-high', 'effort': 'high'}}
        self.assertEqual(validate_route(config, self.options), config)
        config['vision']['effort'] = 'max'
        with self.assertRaisesRegex(ValueError, '不支援 effort'):
            validate_route(config, self.options)

    def test_sol_61_rejects_unsupported_reasoning_efforts(self):
        config = {'version': 1,
                  'delegation': {'type': 'responses', 'model': 'gpt-6.1-sol', 'effort': 'low'},
                  'vision': {'provider': 'openai', 'model': 'gpt-6-luna', 'effort': 'max'}}
        for effort in ('none', 'minimal'):
            with self.subTest(effort=effort):
                config['delegation']['effort'] = effort
                with self.assertRaisesRegex(ValueError, '不支援 effort'):
                    validate_route(config, self.options)

    def test_image_model_must_support_image(self):
        self.options['codex']['models'][0]['image'] = False
        config = {'version': 1,
                  'delegation': {'type': 'responses', 'model': 'gpt-6.1-sol', 'effort': 'low'},
                  'vision': {'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'low'}}
        with self.assertRaisesRegex(ValueError, '圖片輸入'):
            validate_route(config, self.options)


class ModelTransmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_turn_receives_selected_model_and_effort(self):
        with tempfile.TemporaryDirectory() as directory:
            client=CodexClient(Path(directory),model='gpt-6.1-sol',effort='low')
            client.thread_id='thread-1'
            calls=[]
            async def request(method,params,timeout=40):
                calls.append((method,params))
                return {'turn':{'id':'turn-1'}}
            client.request=request
            client.events.put_nowait({'method':'turn/completed','params':{
                'threadId':'thread-1','turn':{'id':'turn-1','status':'completed',
                    'items':[{'type':'agentMessage','text':'ok'}]}}})
            self.assertEqual(await client.turn('help'),'ok')
            self.assertEqual(calls[0][1]['model'],'gpt-6.1-sol')
            self.assertEqual(calls[0][1]['effort'],'low')

    async def test_agy_receives_model_and_effort_flags(self):
        class Process:
            returncode=0
            async def communicate(self):return b'{"status":"SUCCESS"}',b''
        with tempfile.TemporaryDirectory() as directory:
            captured=[]
            async def spawn(*args,**kwargs):
                captured.extend(args)
                return Process()
            with patch('server.providers.shutil.which',return_value='agy'),\
                 patch('server.providers.asyncio.create_subprocess_exec',side_effect=spawn):
                await AgyClient().turn('help',Path(directory),model='gemini-flash-high',effort='high')
            self.assertEqual(captured[captured.index('--model')+1],'gemini-flash-high')
            self.assertEqual(captured[captured.index('--effort')+1],'high')

    async def test_independent_vision_request_uses_selected_model_effort_and_image(self):
        captured=[]
        def handler(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200,json={'id':'resp-1','status':'completed',
                'output':[{'type':'message','content':[{'type':'output_text','text':'{"evidence":[]}'}]}],
                'usage':{'input_tokens':10,'output_tokens':4}})
        provider=OpenAIResponseVision('key','gpt-6-luna','max')
        await provider.http.aclose()
        provider.http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        schema={'type':'object','properties':{'evidence':{'type':'array','items':{'type':'string'}}},
                'required':['evidence'],'additionalProperties':False}
        result=await provider.turn('看圖',image_url='data:image/jpeg;base64,/9j/',schema=schema)
        self.assertEqual(json.loads(result),{'evidence':[]})
        self.assertEqual(captured[0]['model'],'gpt-6-luna')
        self.assertEqual(captured[0]['reasoning']['effort'],'max')
        self.assertEqual(captured[0]['input'][0]['content'][1]['type'],'input_image')
        self.assertEqual(captured[0]['input'][0]['content'][1]['detail'],'original')
        self.assertFalse(captured[0]['store'])
        self.assertEqual(len(provider.usage),1)
        await provider.close()


class RuntimeSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_uses_selected_providers_and_closes_shared_vision_once(self):
        from . import app
        from .test_support import SeededStore as Store

        class ClientTeacher:
            instances=[]
            def __init__(self,workspace,model,effort):
                self.model=model;self.effort=effort;self.closed=0
                self.instances.append(self)
            async def ensure_ready(self):pass
            async def close(self):self.closed+=1

        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=Store(root)
            sid=store.start('student-2','gpt','')
            base={'id':sid,'student_id':'student-2','mode':'gpt','backend':'gpt_configured',
                  'notes':'','teacher_name':'老師','edge_voice':''}
            vision={'provider':'openai','model':'gpt-6-luna','effort':'max'}
            audit=Mock()
            with patch.object(app,'ROOT',root),patch.object(app,'store',store),\
                 patch.object(app,'keys',{'OPENAI_API_KEY':'test-key'}),\
                 patch.object(app,'audits',{sid:audit}),\
                 patch('server.providers.LazyCodex',ClientTeacher):
                responses={**base,'gpt_live':{'version':1,'delegation':{
                    'type':'responses','model':'gpt-6.1-sol','effort':'low'},'vision':vision}}
                runtime=await app.create_runtime(responses)
                self.assertEqual(runtime.provider.setup()['session']['delegation']['type'],'responses')
                self.assertIs(runtime.teacher.provider,runtime.observer.provider)
                self.assertEqual(runtime.observer.provider.model,'gpt-6-luna')
                self.assertEqual(runtime.observer.provider.effort,'max')
                await runtime.close()
                client={**base,'gpt_live':{'version':1,'delegation':{
                    'type':'client','provider':'codex','model':'gpt-6.1-sol','effort':'medium'},
                    'vision':vision}}
                runtime=await app.create_runtime(client)
                self.assertEqual(runtime.provider.setup()['session']['delegation']['type'],'client')
                self.assertEqual(runtime.teacher.provider.model,'gpt-6.1-sol')
                self.assertEqual(runtime.teacher.provider.effort,'medium')
                self.assertEqual(runtime.observer.provider.model,'gpt-6-luna')
                await runtime.close()
                self.assertEqual(ClientTeacher.instances[0].closed,1)
