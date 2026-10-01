import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from .providers import AgyClient, AgyTutor, CodexClient, FailoverTutor, LazyCodex, ProviderError, codex_windows_sandbox_mode
from .tutor import DECISION_SCHEMA


class CodexWindowsSandboxTests(unittest.TestCase):
    def test_basic_defaults_to_unelevated_on_windows(self):
        with patch.dict('os.environ',{},clear=True):
            self.assertEqual(codex_windows_sandbox_mode(True,False,'nt'),'unelevated')
            self.assertIsNone(codex_windows_sandbox_mode(False,False,'nt'))
            self.assertIsNone(codex_windows_sandbox_mode(True,False,'posix'))

    def test_strict_uses_elevated(self):
        with patch.dict('os.environ',{'TUTOR_CODEX_WINDOWS_SANDBOX':'unelevated'}):
            self.assertEqual(codex_windows_sandbox_mode(True,True,'nt'),'elevated')

    def test_invalid_override_fails(self):
        with patch.dict('os.environ',{'TUTOR_CODEX_WINDOWS_SANDBOX':'off'}):
            with self.assertRaises(ProviderError):
                codex_windows_sandbox_mode(True,False,'nt')


class AgyVisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_agy_uses_accept_edits_without_plan(self):
        class Process:
            returncode=0
            async def communicate(self):
                return b'{"status":"SUCCESS","response":"TUTOR_READY"}',b''
        with tempfile.TemporaryDirectory() as folder, \
             patch('server.providers.shutil.which',return_value='agy.exe'), \
             patch('server.providers.asyncio.create_subprocess_exec',return_value=Process()) as launch:
            await AgyClient().turn('synthetic',Path(folder))
            command=launch.call_args.args
            self.assertEqual(command[command.index('--mode')+1],'accept-edits')
            self.assertNotIn('plan',command)
            self.assertIn('--sandbox',command)

    async def test_image_reaches_agy_and_is_removed_after_turn(self):
        with tempfile.TemporaryDirectory() as folder:
            tutor=AgyTutor(Path(folder))
            image=b'\x89PNG\r\n\x1a\nsynthetic-image'
            image_url='data:image/png;base64,'+base64.b64encode(image).decode()
            async def fake_turn(prompt,workspace,**kwargs):
                captures=list(workspace.glob('capture-*.png'))
                self.assertEqual(len(captures),1)
                self.assertEqual(captures[0].read_bytes(),image)
                self.assertIn(captures[0].name,prompt)
                self.assertIn('只輸出 JSON',prompt)
                return {'status':'SUCCESS','conversation_id':'synthetic-conversation',
                        'response':'{"evidence":["7 + 5 = ?"]}'}
            with patch('server.providers.AgyClient.turn',side_effect=fake_turn):
                self.assertEqual(await tutor.turn('觀察作業',image_url=image_url,schema=DECISION_SCHEMA),'{"evidence":["7 + 5 = ?"]}')
            self.assertEqual(list(Path(folder).glob('capture-*')),[])

    async def test_agy_reuses_exact_conversation_and_recovers_changed_id(self):
        with tempfile.TemporaryDirectory() as folder:
            tutor=AgyTutor(Path(folder))
            results=[{'status':'SUCCESS','conversation_id':'same-id','response':'TUTOR_READY'},
                     {'status':'SUCCESS','conversation_id':'same-id','response':'記得前文'},
                     {'status':'SUCCESS','conversation_id':'wrong-id','response':'另一段對話'}]
            async def fake_turn(prompt,workspace,**kwargs):
                expected=None if len(results)==3 else 'same-id'
                self.assertEqual(kwargs['conversation_id'],expected)
                return results.pop(0)
            with patch('server.providers.AgyClient.turn',side_effect=fake_turn):
                await tutor.ensure_ready()
                self.assertEqual(await tutor.turn('接著上課'),'記得前文')
                with self.assertRaisesRegex(ProviderError,'對話識別改變'):
                    await tutor.turn('下一題')
                self.assertIsNone(tutor.conversation_id)
                self.assertEqual(tutor.conversation_generation,1)

    async def test_agy_cancellation_restarts_next_conversation(self):
        with tempfile.TemporaryDirectory() as folder:
            tutor=AgyTutor(Path(folder));tutor.conversation_id='interrupted-id'
            with patch('server.providers.AgyClient.turn',side_effect=asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await tutor.turn('synthetic')
            self.assertIsNone(tutor.conversation_id)
            self.assertEqual(tutor.conversation_generation,1)

    async def test_failed_turn_still_removes_image(self):
        with tempfile.TemporaryDirectory() as folder:
            tutor=AgyTutor(Path(folder))
            image_url='data:image/jpeg;base64,'+base64.b64encode(b'\xff\xd8\xffsynthetic').decode()
            with patch('server.providers.AgyClient.turn',side_effect=ProviderError('agy failed')):
                with self.assertRaisesRegex(ProviderError,'agy failed'):
                    await tutor.turn('觀察作業',image_url=image_url)
            self.assertEqual(list(Path(folder).glob('capture-*')),[])

    async def test_invalid_image_never_calls_agy(self):
        with tempfile.TemporaryDirectory() as folder:
            tutor=AgyTutor(Path(folder))
            with patch('server.providers.AgyClient.turn') as turn:
                with self.assertRaises(ProviderError):
                    await tutor.turn('觀察作業',image_url='data:image/png;base64,invalid!')
                turn.assert_not_called()

    async def test_basic_agy_uses_selected_archived_photo(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder,'AGENTS.md').write_text('synthetic tutor rules',encoding='utf-8')
            tutor=AgyTutor(Path(folder))
            async def fake_turn(prompt,workspace,**kwargs):
                self.assertIn('selected_photo.archive_path',prompt)
                self.assertIn('internal/photos/first.jpg',prompt)
                self.assertNotIn('AGENTS.md 指出的最新照片',prompt)
                return {'status':'SUCCESS','conversation_id':'same-id','response':'{"evidence":[]}'}
            with patch('server.providers.AgyClient.turn',side_effect=fake_turn):
                reply=await tutor.turn('資料：{"selected_photo":{"archive_path":"internal/photos/first.jpg"}}')
            self.assertEqual(reply,'{"evidence":[]}')


class BasicReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_restarts_unsynchronized_conversation(self):
        with tempfile.TemporaryDirectory() as folder, patch('server.providers.CodexClient') as client_type:
            old=client_type.return_value
            old.synchronized=False
            old.close=AsyncMock()
            new=SimpleNamespace(start=AsyncMock(),close=AsyncMock(),
                                account_status=AsyncMock(return_value={'authenticated':True}),
                                turn=AsyncMock(return_value='TUTOR_READY'))
            client_type.side_effect=[old,new]
            tutor=LazyCodex(Path(folder));tutor.started=True
            await tutor.ensure_conversation()
            old.close.assert_awaited_once()
            new.start.assert_awaited_once()
            self.assertIs(tutor.client,new)
            self.assertEqual(tutor.conversation_generation,1)

    async def test_codex_starts_one_thread_for_multiple_turns(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder,'AGENTS.md').write_text('synthetic class rules',encoding='utf-8')
            client=CodexClient(Path(folder));calls=[]
            async def request(method,params,timeout=40):
                calls.append((method,params))
                if method=='thread/start':return {'thread':{'id':'same-thread'}}
                index=sum(name=='turn/start' for name,_ in calls)
                client.events.put_nowait({'method':'turn/completed','params':{
                    'threadId':'same-thread','turn':{'id':f'turn-{index}',
                    'status':'completed','items':[{'type':'agentMessage','text':f'答覆 {index}'}]}}})
                return {'turn':{'id':f'turn-{index}'}}
            client.request=request
            self.assertEqual(await client.turn('第一題'),'答覆 1')
            self.assertEqual(await client.turn('接續上一題'),'答覆 2')
            self.assertEqual([name for name,_ in calls].count('thread/start'),1)
            self.assertEqual([params['threadId'] for name,params in calls if name=='turn/start'],
                             ['same-thread','same-thread'])
            start=next(params for name,params in calls if name=='thread/start')
            self.assertEqual(start['permissions'],':workspace')
            self.assertNotIn('sandbox',start)
            self.assertTrue(start['config']['features.shell_tool'])
            self.assertEqual([params['permissions'] for name,params in calls if name=='turn/start'],
                             [':workspace',':workspace'])
            self.assertTrue(all('sandboxPolicy' not in params for name,params in calls if name=='turn/start'))

    async def test_codex_strict_profile_is_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ',{'TUTOR_CODEX_STRICT_PERMISSIONS':'1'}):
            Path(folder,'AGENTS.md').write_text('synthetic class rules',encoding='utf-8')
            client=CodexClient(Path(folder));calls=[]
            self.assertTrue(client.strict_permissions)
            async def request(method,params,timeout=40):
                calls.append((method,params))
                if method=='thread/start':return {'thread':{'id':'strict-thread'}}
                client.events.put_nowait({'method':'turn/completed','params':{
                    'threadId':'strict-thread','turn':{'id':'strict-turn','status':'completed',
                    'items':[{'type':'agentMessage','text':'完成'}]}}})
                return {'turn':{'id':'strict-turn'}}
            client.request=request
            self.assertEqual(await client.turn('測試'),'完成')
            start=next(params for name,params in calls if name=='thread/start')
            turn=next(params for name,params in calls if name=='turn/start')
            self.assertEqual(start['permissions'],'tutor')
            self.assertEqual(turn['permissions'],'tutor')
            self.assertNotIn('sandbox',start)
            self.assertNotIn('sandboxPolicy',turn)

    async def test_codex_without_basic_workspace_remains_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            client=CodexClient(Path(folder));calls=[]
            async def request(method,params,timeout=40):
                calls.append((method,params))
                if method=='thread/start':return {'thread':{'id':'text-thread'}}
                client.events.put_nowait({'method':'turn/completed','params':{
                    'threadId':'text-thread','turn':{'id':'text-turn','status':'completed',
                    'items':[{'type':'agentMessage','text':'完成'}]}}})
                return {'turn':{'id':'text-turn'}}
            client.request=request
            self.assertEqual(await client.turn('測試'),'完成')
            start=next(params for name,params in calls if name=='thread/start')
            turn=next(params for name,params in calls if name=='turn/start')
            self.assertEqual(start['sandbox'],'read-only')
            self.assertFalse(start['config']['features.shell_tool'])
            self.assertEqual(turn['sandboxPolicy'],{'type':'readOnly','networkAccess':False})

    async def test_codex_requires_authenticated_reply(self):
        with tempfile.TemporaryDirectory() as folder, patch('server.providers.CodexClient') as client_type:
            client=client_type.return_value
            client.start=AsyncMock();client.close=AsyncMock()
            client.account_status=AsyncMock(return_value={'authenticated':True})
            client.turn=AsyncMock(return_value='TUTOR_READY')
            tutor=LazyCodex(Path(folder))
            await tutor.ensure_ready()
            self.assertTrue(tutor.started)
            client.turn.assert_awaited_once()
            await tutor.close()
            client.account_status=AsyncMock(return_value={'authenticated':False})
            other=LazyCodex(Path(folder))
            with self.assertRaisesRegex(ProviderError,'尚未登入'):
                await other.ensure_ready()
            self.assertFalse(other.started)
            client.close.assert_awaited()


class LiveFailoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_switches_both_directions_and_rejects_invalid_output(self):
        valid='{"quiet":true,"speech":"","board_html":"","close_board":false,"need_capture":false,"evidence":[]}'
        class Stub:
            def __init__(self,outcomes):self.outcomes=outcomes;self.calls=0
            async def turn(self,*args,**kwargs):
                self.calls+=1
                outcome=self.outcomes.pop(0)
                if isinstance(outcome,Exception):raise outcome
                return outcome
            async def close(self):pass
        agy=Stub([ProviderError('agy offline'),valid,'invalid json'])
        codex=Stub([valid,ProviderError('codex offline'),valid])
        with tempfile.TemporaryDirectory() as folder, \
             patch('server.providers.AgyTutor',return_value=agy), \
             patch('server.providers.LazyCodex',return_value=codex):
            tutor=FailoverTutor(Path(folder))
            for expected in ('codex','agy','codex'):
                self.assertEqual(await tutor.turn('觀察',image_url='synthetic',schema=DECISION_SCHEMA),valid)
                self.assertEqual(tutor.active,expected)
            self.assertEqual((agy.calls,codex.calls),(3,3))
            await tutor.close()

    async def test_both_failed_returns_error(self):
        class Offline:
            async def turn(self,*args,**kwargs):raise ProviderError('offline')
            async def close(self):pass
        with tempfile.TemporaryDirectory() as folder, \
             patch('server.providers.AgyTutor',return_value=Offline()), \
             patch('server.providers.LazyCodex',return_value=Offline()):
            tutor=FailoverTutor(Path(folder))
            with self.assertRaisesRegex(ProviderError,'均無法完成'):
                await tutor.turn('觀察',schema=DECISION_SCHEMA)


if __name__=='__main__':unittest.main()
