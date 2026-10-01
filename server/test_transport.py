from .test_support import install_test_seed
import asyncio
import importlib
import os
import tempfile
import unittest
import json
import base64
from unittest.mock import patch
from types import SimpleNamespace
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from .basic import BasicRuntime
from .tutor import TutorSession
from .test_basic import Teacher,Speech
from .transport import RuntimeBridge

class BackpressureTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_native_image_message_is_not_rejected_by_old_transport_cap(self):
        image = 'data:image/png;base64,' + 'A' * 5_400_000
        class Runtime:
            session=SimpleNamespace(id='native-png',generation=0)
            received=None
            def accept_frame(self,value,request_id=None,capture_meta=None):self.received=value
            async def close(self):return {'closed':True}
        class Socket:
            step=0
            async def accept(self):pass
            async def receive_json(self):return {'type':'authenticate','token':'secret'}
            async def send_json(self,event):pass
            async def receive_text(self):
                self.step+=1
                return json.dumps({'type':'frame','session_id':'native-png','image':image} if self.step==1
                                  else {'type':'close','session_id':'native-png'})
            async def close(self,code):pass
        runtime=Runtime()
        await RuntimeBridge(runtime).serve(Socket(),'secret')
        self.assertEqual(runtime.received,image)

    async def test_transport_disconnect_can_resume_same_runtime(self):
        class Runtime:
            session=SimpleNamespace(id='resumable',generation=0,basic_workflow=True)
            close_count=0
            start_count=0
            mic=True
            camera=True
            photo_preserved=False
            async def start(self):self.start_count+=1
            def set_mic(self,value):self.mic=value
            def set_camera(self,value,preserve_photo=False):
                self.camera=value;self.photo_preserved=preserve_photo
            async def interrupt(self,receipt):self.session.generation+=1
            async def close(self):
                self.close_count+=1
                return {'closed':True}

        class Socket:
            def __init__(self,disconnect):self.disconnect=disconnect;self.events=[]
            async def accept(self):pass
            async def receive_json(self):return {'type':'authenticate','token':'secret'}
            async def send_json(self,event):self.events.append(event)
            async def receive_text(self):
                if self.disconnect:raise WebSocketDisconnect(code=1006)
                return '{"type":"close","session_id":"resumable"}'
            async def close(self,code):pass

        runtime=Runtime();bridge=RuntimeBridge(runtime)
        first=Socket(True)
        await bridge.serve(first,'secret')
        self.assertFalse(bridge.closed)
        self.assertFalse(runtime.mic)
        self.assertFalse(runtime.camera)
        self.assertTrue(runtime.photo_preserved)
        self.assertEqual(runtime.close_count,0)
        second=Socket(False)
        await bridge.serve(second,'secret')
        self.assertTrue(second.events[0]['resumed'])
        self.assertEqual(runtime.start_count,1)
        self.assertEqual(runtime.close_count,1)

    async def test_unrecovered_transport_expires_runtime(self):
        class Runtime:
            session=SimpleNamespace(id='expire',generation=0)
            closed=0
            def set_mic(self,value):pass
            def set_camera(self,value):pass
            async def interrupt(self,receipt):pass
            async def close(self):
                self.closed+=1
                return {'closed':True}
        class Socket:
            async def accept(self):pass
            async def receive_json(self):return {'type':'authenticate','token':'secret'}
            async def send_json(self,event):pass
            async def receive_text(self):raise WebSocketDisconnect(code=1006)
            async def close(self,code):pass
        runtime=Runtime();bridge=RuntimeBridge(runtime);bridge.reconnect_seconds=0.01
        await bridge.serve(Socket(),'secret')
        await asyncio.wait_for(bridge.expiry,1)
        self.assertTrue(bridge.closed)
        self.assertEqual(runtime.closed,1)

    async def test_output_overflow_closes_idle_socket_and_runtime(self):
        class Runtime:
            session=SimpleNamespace(id='overflow-test',generation=0)
            close_count=0
            async def close(self):
                self.close_count+=1
                return {'closed':True}

        class Socket:
            def __init__(self):
                self.ready=asyncio.Event();self.close_code=None
            async def accept(self):pass
            async def receive_json(self):return {'type':'authenticate','token':'secret'}
            async def send_json(self,event):
                if event['type']=='ready':self.ready.set()
            async def receive_text(self):await asyncio.Event().wait()
            async def close(self,code):self.close_code=code

        runtime=Runtime();bridge=RuntimeBridge(runtime);socket=Socket()
        serving=asyncio.create_task(bridge.serve(socket,'secret'))
        await asyncio.wait_for(socket.ready.wait(),1)
        for index in range(64):await bridge.emit({'type':'event','index':index})
        with self.assertRaisesRegex(RuntimeError,'桌面處理不及'):
            await bridge.emit({'type':'event','index':64})
        await asyncio.wait_for(serving,1)
        self.assertEqual(socket.close_code,1013)
        self.assertEqual(runtime.close_count,1)

    async def test_external_close_wakes_idle_socket(self):
        class Runtime:
            session=SimpleNamespace(id='external-close',generation=0)
            async def close(self):return {'closed':True}

        class Socket:
            def __init__(self):self.ready=asyncio.Event();self.close_code=None
            async def accept(self):pass
            async def receive_json(self):return {'type':'authenticate','token':'secret'}
            async def send_json(self,event):
                if event['type']=='ready':self.ready.set()
            async def receive_text(self):await asyncio.Event().wait()
            async def close(self,code):self.close_code=code

        bridge=RuntimeBridge(Runtime());socket=Socket()
        serving=asyncio.create_task(bridge.serve(socket,'secret'))
        await asyncio.wait_for(socket.ready.wait(),1)
        await bridge.close()
        await asyncio.wait_for(serving,1)
        self.assertEqual(socket.close_code,1000)

class TransportTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();os.environ['TUTOR_DATA_DIR']=self.directory.name;install_test_seed(self.directory.name);os.environ['TUTOR_CORE_TOKEN']='transport-test'
        from . import app
        self.module=importlib.reload(app)
        async def factory(state):
            async def emit(event):pass
            return BasicRuntime(TutorSession(state['student_id'],2,id=state['id']),Teacher(),Teacher(),Speech(),emit,lambda *args:None)
        self.module.runtime_factory=factory
        self.client=TestClient(self.module.app);self.client.__enter__()
        self.headers={'Authorization':'Bearer transport-test'}
        result=self.client.post('/sessions',headers=self.headers,json={'student_id':'student-2','mode':'basic'})
        self.sid=result.json()['id'];self.assertTrue(result.json()['online'])
    def tearDown(self):
        self.client.__exit__(None,None,None);self.directory.cleanup()
    def test_large_http_png_preserves_bytes_and_capture_waiter_while_websocket_audio_continues(self):
        runtime=self.module.bridge.runtime
        runtime.mode='gpt'
        runtime.set_camera(True)
        runtime.observations.push=lambda frame:None
        decoded=b'\x89PNG\r\n\x1a\n'+b'\0'*6_500_000
        image='data:image/png;base64,'+base64.b64encode(decoded).decode('ascii')
        received=[]
        async def make_waiter():
            waiter=asyncio.get_running_loop().create_future()
            runtime.capture_waiters['fresh-check']=waiter
            return waiter
        waiter=self.client.portal.call(make_waiter)
        with patch.object(runtime,'accept_audio',side_effect=lambda pcm: received.append(pcm)):
            with self.client.websocket_connect(f'/sessions/{self.sid}/stream') as ws:
                ws.send_json({'type':'authenticate','token':'transport-test'});ws.receive_json()
                result=self.client.post(f'/sessions/{self.sid}/frame',headers=self.headers,
                    json={'image':image,'request_id':'fresh-check','capture_meta':{'source_width':3840,'source_height':2160,
                        'capture_method':'windows-native-photo','captured_at':'2026-10-01T00:00:00Z'}})
                self.assertEqual(result.status_code,200,result.text)
                self.assertEqual(runtime.frame['image'],image)
                self.assertEqual(runtime.frame['capture_meta']['capture_method'],'windows-native-photo')
                self.assertEqual(runtime.frame['capture_meta']['captured_at'],'2026-10-01T00:00:00Z')
                self.assertEqual(waiter.result()['id'],runtime.frame['id'])
                ws.send_json({'type':'audio','session_id':self.sid,'pcm':'AAA='})
                ws.send_json({'type':'text','session_id':self.sid,'text':'你好'})
                self.assertEqual(ws.receive_json()['type'],'transcript')
                self.assertEqual(received,[b'\x00\x00'])
                ws.send_json({'type':'close','session_id':self.sid})

    def test_http_photo_rejects_wrong_auth_session_closed_camera_and_invalid_image(self):
        runtime=self.module.bridge.runtime;runtime.mode='gpt';runtime.set_camera(True)
        target=f'/sessions/{self.sid}/frame'
        body={'image':'data:image/png;base64,AA=='}
        self.assertEqual(self.client.post(target,json=body).status_code,401)
        self.assertEqual(self.client.post('/sessions/wrong/frame',headers=self.headers,json=body).status_code,409)
        with self.client.websocket_connect(f'/sessions/{self.sid}/stream') as ws:
            ws.send_json({'type':'authenticate','token':'transport-test'});ws.receive_json()
            self.assertEqual(self.client.post(target,headers=self.headers,json=body).status_code,422)
            runtime.set_camera(False)
            self.assertEqual(self.client.post(target,headers=self.headers,json=body).status_code,409)
            ws.send_json({'type':'close','session_id':self.sid})
        self.assertEqual(self.client.post(target,headers=self.headers,json=body).status_code,409)
    def test_auth_failure_cannot_cancel_the_connected_session(self):
        with self.client.websocket_connect(f'/sessions/{self.sid}/stream') as ws:
            ws.send_json({'type':'authenticate','token':'transport-test'});self.assertEqual(ws.receive_json()['type'],'ready')
            with self.client.websocket_connect(f'/sessions/{self.sid}/stream') as wrong:
                wrong.send_json({'type':'authenticate','token':'wrong'})
                self.assertEqual(wrong.receive()['code'],4401)
            ws.send_json({'type':'audio','session_id':self.sid,'pcm':'AAA='})
            events=[]
            while not events or events[-1]['type']!='speech':
                events.append(ws.receive_json())
            self.assertEqual([e['type'] for e in events if e['type']!='audio_status'],
                             ['transcript','thinking','board','speech'])
            ws.send_json({'type':'close','session_id':self.sid});self.assertEqual(ws.receive_json()['type'],'closed')

    def test_live_audio_uses_streaming_signature_without_basic_receipt(self):
        runtime=self.module.bridge.runtime
        runtime.mode='gpt'
        received=[]
        with patch.object(runtime,'accept_audio',side_effect=lambda pcm: received.append(pcm)):
            with self.client.websocket_connect(f'/sessions/{self.sid}/stream') as ws:
                ws.send_json({'type':'authenticate','token':'transport-test'})
                self.assertEqual(ws.receive_json()['type'],'ready')
                ws.send_json({'type':'audio','session_id':self.sid,'pcm':'AAA='})
                ws.send_json({'type':'text','session_id':self.sid,'text':'你好'})
                self.assertEqual(ws.receive_json()['type'],'transcript')
                self.assertEqual(received,[b'\x00\x00'])
                ws.send_json({'type':'close','session_id':self.sid})
    def test_wrong_session_message_closes_only_its_transport(self):
        with self.client.websocket_connect(f'/sessions/{self.sid}/stream') as ws:
            ws.send_json({'type':'authenticate','token':'transport-test'});ws.receive_json()
            ws.send_json({'type':'audio','session_id':'another-student','pcm':'AAA='})
            self.assertEqual(ws.receive()['code'],4400)

    def test_busy_audio_and_invalid_user_input_keep_session_connected(self):
        with self.client.websocket_connect(f'/sessions/{self.sid}/stream') as ws:
            ws.send_json({'type':'authenticate','token':'transport-test'})
            self.assertEqual(ws.receive_json()['type'],'ready')
            with patch.object(self.module.bridge.runtime,'accept_audio',side_effect=RuntimeError('老師正在處理前面的話，請稍候再說一次。')):
                ws.send_json({'type':'audio','session_id':self.sid,'pcm':'AAA='})
                self.assertEqual(ws.receive_json()['stage'],'rejected')
                self.assertEqual(ws.receive_json()['type'],'error')
            ws.send_json({'type':'frame','session_id':self.sid,'image':'bad'})
            self.assertEqual(ws.receive_json()['type'],'error')
            ws.send_json({'type':'text','session_id':self.sid,'text':''})
            self.assertEqual(ws.receive_json()['type'],'error')
            ws.send_json({'type':'text','session_id':self.sid,'text':'你好'})
            self.assertEqual(ws.receive_json()['type'],'transcript')
            ws.send_json({'type':'close','session_id':self.sid})

    def test_expired_transport_releases_session_for_next_lesson(self):
        bridge=self.module.bridge
        bridge.reconnect_seconds=0
        self.client.portal.call(bridge.expire_detached)
        self.assertIsNone(self.module.active)
        history=self.client.get('/students/student-2/history',headers=self.headers).json()
        session=next(item for item in history['sessions'] if item['id']==self.sid)
        self.assertEqual(session['status'],'interrupted')
        self.assertEqual(sum(item['kind']=='session_cost' and item['session_id']==self.sid
                             for item in history['events']),1)
        response=self.client.post('/sessions',headers=self.headers,
                                  json={'student_id':'student-2','mode':'basic'})
        self.assertEqual(response.status_code,200)
        self.client.post(f"/sessions/{response.json()['id']}/end",headers=self.headers)


if __name__=='__main__':unittest.main()
