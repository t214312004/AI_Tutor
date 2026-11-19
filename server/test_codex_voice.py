import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from .codex_voice import CodexVoiceClient, SubscriptionCodex
from .providers import ProviderError


class Pipe:
    def __init__(self):self.queue=asyncio.Queue()
    async def readline(self):return await self.queue.get()
    async def read(self,size):return await self.queue.get()


class Process:
    def __init__(self):
        self.returncode=None;self.stdout=Pipe();self.stderr=Pipe();self.messages=[]
        self.stdin=self
    def write(self,data):
        value=json.loads(data);self.messages.append(value)
        if value.get('method')=='local.inspect':
            self.stdout.queue.put_nowait((json.dumps({'id':value['id'],'result':{'observed':True}})+'\n').encode())
    async def drain(self):pass
    def close(self):self.returncode=0
    async def wait(self):self.returncode=0;return 0
    def kill(self):self.returncode=0


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_tool_can_await_rpc_without_deadlocking_stdout_reader(self):
        client=CodexVoiceClient(Path('.'));client.process=Process()
        async def tool(params):
            observed=await client.request('local.inspect',{},timeout=1)
            return {'success':observed['observed'],'contentItems':[]}
        client.tool=tool;client.reader=asyncio.create_task(client.read())
        client.process.stdout.queue.put_nowait((json.dumps({'id':'server-call','method':'item/tool/call',
            'params':{'tool':'inspect'}})+'\n').encode())
        for _ in range(100):
            if any(m.get('id')=='server-call' for m in client.process.messages):break
            await asyncio.sleep(.01)
        self.assertEqual(client.process.messages[-1],{'id':'server-call','result':{'success':True,'contentItems':[]}})
        await client.close()

    async def test_unknown_server_requests_are_denied(self):
        client=CodexVoiceClient(Path('.'));client.process=Process()
        await client.dispatch_tool({'id':'approval','method':'item/commandExecution/requestApproval','params':{}})
        self.assertEqual(client.process.messages[-1]['error']['code'],-32601)
        await client.close()

    async def test_subscription_start_removes_environment_and_stored_api_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'private';source.mkdir()
            (source/'auth.json').write_text(json.dumps({'tokens':{'access_token':'synthetic'},'OPENAI_API_KEY':'synthetic-key'}))
            client=CodexVoiceClient(Path(directory)/'workspace');process=Process()
            async def request(method,params,**kwargs):
                if method=='account/read':return {'account':{'type':'chatgpt'}}
                if method=='thread/start':return {'thread':{'id':'synthetic'}}
                return {}
            with patch('server.codex_voice.codex_home',return_value=source),\
                patch.dict(os.environ,{'OPENAI_API_KEY':'synthetic','CODEX_ACCESS_TOKEN':'synthetic',
                                       'OPENAI_BASE_URL':'https://example.invalid','TUTOR_CORE_TOKEN':'local-only'}),\
                patch('server.codex_voice.asyncio.create_subprocess_exec',AsyncMock(return_value=process)) as launch,\
                patch.object(client,'request',side_effect=request):
                await client.ensure_ready('合成測試',[])
                env=launch.call_args.kwargs['env']
                self.assertFalse(any(key in env for key in ('OPENAI_API_KEY','CODEX_ACCESS_TOKEN','OPENAI_BASE_URL','TUTOR_CORE_TOKEN')))
                home=Path(client.home.name)
                self.assertNotIn('OPENAI_API_KEY',json.loads((home/'auth.json').read_text()))
                self.assertIn('features.shell_tool=false',launch.call_args.args)
                self.assertEqual(client.thread_id,'synthetic')
                await client.close()
                self.assertFalse(home.exists())
                self.assertIn('OPENAI_API_KEY',json.loads((source/'auth.json').read_text()))

    async def test_api_key_only_login_is_rejected_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory);(source/'auth.json').write_text('{"OPENAI_API_KEY":"synthetic"}')
            client=CodexVoiceClient(source/'workspace')
            with patch('server.codex_voice.codex_home',return_value=source),\
                patch('server.codex_voice.asyncio.create_subprocess_exec') as launch:
                with self.assertRaises(ProviderError):await client.ensure_ready('合成測試',[])
                launch.assert_not_called()

    async def test_image_model_buffers_early_events_and_ignores_retired_turns(self):
        provider=SubscriptionCodex(Path('.'),'synthetic')
        provider.client.thread_id='vision'
        provider.ensure_ready=AsyncMock()
        provider.client.close=AsyncMock()
        async def request(method,params):
            for ident,text in [('retired','不能混入的舊照片'),('current','37 + 28')]:
                provider.notify('turn/started',{'threadId':'vision','turn':{'id':ident}})
                provider.notify('item/agentMessage/delta',{'threadId':'vision','turnId':ident,'delta':text})
                provider.notify('turn/completed',{'threadId':'vision','turn':{'id':ident,'status':'completed'}})
            return {'turn':{'id':'current'}}
        provider.client.request=request
        image = 'data:image/png;base64,iVBORw0KGgo='
        self.assertEqual(await provider.turn('核對合成圖片', image_url=image),'37 + 28')
        provider.client.close.assert_not_awaited()
        self.assertEqual(await provider.turn('核對第二張合成圖片', image_url=image),'37 + 28')
        provider.client.close.assert_awaited_once()
        self.assertIsNone(provider.future)

    async def test_image_start_cancellation_closes_unknown_turn_and_clears_pending_state(self):
        provider=SubscriptionCodex(Path('.'),'synthetic')
        provider.ensure_ready=AsyncMock();provider.client.close=AsyncMock()
        entered=asyncio.Event()
        async def request(*args,**kwargs):
            entered.set();await asyncio.Event().wait()
        provider.client.request=request
        task=asyncio.create_task(provider.turn('核對合成圖片'))
        await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        provider.client.close.assert_awaited_once()
        self.assertIsNone(provider.future)
        self.assertFalse(provider.starting)
