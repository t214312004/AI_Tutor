"""Authenticated, session-scoped websocket bridge for a provisioned runtime.

Runtime provisioning is separate from transport. The desktop never chooses a
credential, filesystem permission policy, or an eligibility override over this API.
"""
import asyncio
import base64
import json
import logging
from fastapi import WebSocket,WebSocketDisconnect
from .diagnostics import report_failure
from .providers import ProviderError

logger=logging.getLogger('tutor.transport')
KNOWN_MESSAGE_KINDS=frozenset((
    'audio','frame','mic','camera','begin','text','activity','lesson','played','interrupt','close',
    'agy_voice_start','agy_audio','agy_tool_confirm',
    'native_voice_offer','native_voice_stop','native_voice_connected','native_voice_select',
    'interaction','audio_delivery',
))

class RuntimeBridge:
    def __init__(self, runtime, on_expire=None):
        self.runtime=runtime;self.outbox=asyncio.Queue(maxsize=64);self.connected=False;self.closed=False
        self.close_lock=asyncio.Lock();self.cost=None;self.stopped=asyncio.Event();self.overflowed=False
        self.started=False;self.detaching=False;self.expiry=None;self.reconnect_seconds=45
        self.on_expire=on_expire
        self._overflow_cleanup=None
        runtime.emit=self.emit
    def arm_expiry(self, timeout=None):
        if self.expiry is None:
            self.expiry=asyncio.create_task(self.expire_detached(timeout))
    async def emit(self,event):
        if self.closed:return
        try:self.outbox.put_nowait(event)
        except asyncio.QueueFull:
            # Unheard audio and student messages must never silently disappear.
            self.closed=True
            self.overflowed=True;self.stopped.set()
            if not self.connected and (self._overflow_cleanup is None or self._overflow_cleanup.done()):
                async def cleanup_detached_overflow():
                    try:
                        if self.on_expire:await self.on_expire()
                        else:await self.close()
                    except Exception as error:
                        logger.info('detached_overflow_cleanup_failed type=%s',type(error).__name__)
                self._overflow_cleanup=asyncio.create_task(cleanup_detached_overflow())
            raise RuntimeError('桌面處理不及，請重新開始陪讀')
    async def close(self):
        async with self.close_lock:
            self.closed=True
            self.stopped.set()
            if self.expiry and self.expiry is not asyncio.current_task():self.expiry.cancel()
            if self.cost is None:self.cost=await self.runtime.close()
            return self.cost
    async def expire_detached(self, timeout=None):
        try:
            await asyncio.sleep(self.reconnect_seconds if timeout is None else timeout)
            if not self.connected and not self.closed:
                # The callback may close this bridge from a different task.
                # Once expiry has fired, it can no longer be cancelled by close().
                self.expiry=None
                self.detaching=True
                logger.info('reconnect_expired')
                try:
                    if self.on_expire:await self.on_expire()
                    else:await self.close()
                finally:self.detaching=False
        except asyncio.CancelledError:pass
        except Exception as error:report_failure('expire_detached',error)
    async def serve(self,ws:WebSocket,token:str):
        await ws.accept()
        sender=None;owns_connection=False;socket_closed=False
        incoming=stopping=None
        close_reason='unknown'
        last_kind='none'
        try:
            authentication=await asyncio.wait_for(ws.receive_json(),5)
            if authentication!={'type':'authenticate','token':token} or not token:
                close_reason='authentication_failed'
                await ws.close(code=4401);return
            if self.connected or self.detaching or self.closed:
                close_reason='session_unavailable'
                await ws.close(code=4409);return
            if self.expiry:self.expiry.cancel();self.expiry=None
            resumed=self.started
            self.connected=True;owns_connection=True
            if not self.started:
                if hasattr(self.runtime,'start'):await self.runtime.start()
                self.started=True
            await ws.send_json({'type':'ready','session_id':self.runtime.session.id,
                                'generation':self.runtime.session.generation,'resumed':resumed,
                                'direction_revision':getattr(self.runtime.session,'direction_revision',0),
                                'work_sequence':getattr(self.runtime.session,'committed_sequence',0),
                                'interaction_mode':getattr(self.runtime.session,'interaction_mode','normal'),
                                'voice_engine':getattr(self.runtime,'voice_engine','classic'),
                                'codex_voice':getattr(self.runtime,'voice',''),
                                'input_speaker':getattr(self.runtime.session,'input_speaker','unknown')})
            if getattr(self.runtime.session,'interaction_mode','normal')!='normal':
                await self.runtime.send({'type':'interaction_policy','mode':self.runtime.session.interaction_mode,
                                        'reason':self.runtime.session.interaction_note})
            logger.info('connection_ready resumed=%s',resumed)
            async def send():
                while not self.closed:
                    event=await self.outbox.get()
                    if not self.closed:await ws.send_json(event)
            sender=asyncio.create_task(send())
            stopping=asyncio.create_task(self.stopped.wait())
            while not self.closed:
                incoming=asyncio.create_task(ws.receive_text())
                done,pending=await asyncio.wait((incoming,stopping,sender),return_when=asyncio.FIRST_COMPLETED)
                if incoming in pending:
                    incoming.cancel()
                    await asyncio.gather(incoming,return_exceptions=True)
                if stopping in done and self.stopped.is_set():
                    close_reason='outbox_overflow' if self.overflowed else 'session_closed'
                    await ws.close(code=1013 if self.overflowed else 1000)
                    socket_closed=True
                    break
                if sender in done:
                    sender.result()
                    raise RuntimeError('桌面傳送連線已中斷')
                raw=incoming.result()
                # Full-resolution PNG frames can exceed the old JPEG-sized cap.
                if len(raw)>16_100_000:raise ValueError('訊息過大')
                message=json.loads(raw)
                if not isinstance(message,dict):raise ValueError('訊息必須為物件')
                if 'receipt' in message and not isinstance(message['receipt'],dict):raise ValueError('無效播放紀錄')
                if message.get('session_id')!=self.runtime.session.id:
                    close_reason='session_mismatch'
                    raise ValueError('陪讀識別不符')
                kind=message.get('type')
                last_kind=kind if kind in KNOWN_MESSAGE_KINDS else 'invalid'
                if kind=='audio':
                    data=base64.b64decode(message.get('pcm',''),validate=True)
                    if len(data)%2 or len(data)>960_000:raise ValueError('無效音訊')
                    utterance_id=message.get('utterance_id')
                    if utterance_id is not None and (not isinstance(utterance_id,str) or len(utterance_id)>64):
                        raise ValueError('無效語音片段識別')
                    audio_meta=message.get('audio_meta')
                    if audio_meta is not None and (not isinstance(audio_meta,dict) or len(json.dumps(audio_meta, ensure_ascii=False))>400):
                        raise ValueError('無效收音資訊')
                    try:
                        if not hasattr(self.runtime,'accept_audio'):
                            raise ValueError('此課程模式不接受傳統音訊串流')
                        if getattr(self.runtime,'mode','basic') in ('gpt','gemini'):
                            self.runtime.accept_audio(data)
                        else:
                            accepted=self.runtime.accept_audio(data,utterance_id,audio_meta)
                            await self.runtime.send({'type':'audio_status','stage':'received' if accepted else 'rejected',
                                                     'utterance_id':utterance_id})
                    except (ValueError,RuntimeError) as error:
                        logger.info('recoverable_message_error kind=audio type=%s',type(error).__name__)
                        if getattr(self.runtime,'mode','basic')=='basic':
                            await self.runtime.send({'type':'audio_status','stage':'rejected',
                                                     'utterance_id':utterance_id})
                        await self.runtime.error(str(error))
                elif kind=='frame':
                    capture_meta=message.get('capture_meta')
                    if capture_meta is not None and (not isinstance(capture_meta,dict) or len(json.dumps(capture_meta, ensure_ascii=False))>1000):
                        raise ValueError('無效拍攝資訊')
                    try:
                        req_id=message.get('request_id')
                        if req_id is not None and (not isinstance(req_id,str) or not 1<=len(req_id)<=64):
                            raise ValueError('無效拍照請求識別')
                        if req_id is None and hasattr(self.runtime,'accept_background_frame'):
                            self.runtime.accept_background_frame(message.get('image'),capture_meta)
                        else:
                            self.runtime.accept_frame(message.get('image'),req_id,capture_meta)
                    except ValueError as error:
                        logger.info('recoverable_message_error kind=frame type=ValueError')
                        await self.runtime.error(str(error))
                elif kind=='agy_voice_start':
                    if getattr(self.runtime,'voice_engine',None)!='agy':raise ValueError('此課程未使用 agy Live')
                    try:await self.runtime.connect_voice(message.get('rtc_id'))
                    except (ValueError,ProviderError) as error:
                        logger.info('recoverable_message_error kind=agy_voice_start type=%s',type(error).__name__)
                        await self.runtime.error(str(error))
                elif kind=='agy_audio':
                    if getattr(self.runtime,'voice_engine',None)!='agy':raise ValueError('此課程未使用 agy Live')
                    encoded = message.get('pcm')
                    if not isinstance(encoded, str) or len(encoded) > 4400:raise ValueError('無效的 agy 音訊')
                    try:self.runtime.accept_media(base64.b64decode(encoded, validate=True), message.get('rtc_id'))
                    except ValueError as error:
                        logger.info('recoverable_message_error kind=agy_audio type=ValueError')
                        await self.runtime.error(str(error))
                elif kind=='agy_tool_confirm':
                    if getattr(self.runtime,'voice_engine',None)!='agy':raise ValueError('此課程未使用 agy Live')
                    try:await self.runtime.confirm_tool(message.get('id'), message.get('approved') is True)
                    except (ValueError,ProviderError) as error:
                        logger.info('recoverable_message_error kind=agy_tool_confirm type=%s',type(error).__name__)
                        await self.runtime.error(str(error))
                elif kind=='native_voice_offer':
                    if getattr(self.runtime,'voice_engine',None)!='codex':raise ValueError('此課程未使用訂閱語音')
                    try:await self.runtime.connect_voice(message.get('sdp'),message.get('rtc_id'))
                    except (ValueError,ProviderError) as error:
                        logger.info('recoverable_message_error kind=native_voice_offer type=%s',type(error).__name__)
                        await self.runtime.error(str(error))
                elif kind=='native_voice_stop':
                    if getattr(self.runtime,'voice_engine',None) not in ('codex','agy'):raise ValueError('此課程未使用訂閱語音')
                    try:await self.runtime.stop_voice()
                    except ProviderError as error:await self.runtime.error(str(error))
                elif kind=='native_voice_connected':
                    if getattr(self.runtime,'voice_engine',None)!='codex':raise ValueError('此課程未使用訂閱語音')
                    await self.runtime.voice_ready(message.get('rtc_id'))
                elif kind=='native_voice_select':
                    if getattr(self.runtime,'voice_engine',None)!='codex':raise ValueError('此課程未使用訂閱語音')
                    try:await self.runtime.select_voice(message.get('voice'))
                    except (ValueError,ProviderError) as error:
                        await self.runtime.stop_voice()
                        await self.runtime.send({'type':'native_voice_selected','voice':self.runtime.voice})
                        await self.runtime.send({'type':'native_voice_status','stage':'error'})
                        await self.runtime.error(str(error))
                elif kind=='mic':self.runtime.set_mic(message.get('enabled') is True)
                elif kind=='camera':self.runtime.set_camera(message.get('enabled') is True)
                elif kind=='begin':self.runtime.begin()
                elif kind=='text':
                    try:self.runtime.accept_text(message.get('text'))
                    except ValueError as error:
                        logger.info('recoverable_message_error kind=text type=ValueError')
                        await self.runtime.error(str(error))
                elif kind=='activity':self.runtime.activity()
                elif kind=='lesson':
                    try:await self.runtime.update_lesson(message.get('action'),message.get('task_id'))
                    except ValueError as error:
                        logger.info('recoverable_message_error kind=lesson type=ValueError')
                        await self.runtime.error(str(error))
                elif kind=='interaction':
                    try:await self.runtime.interaction_control(message.get('mode'),message.get('speaker'))
                    except ValueError as error:await self.runtime.error(str(error))
                elif kind=='audio_delivery':
                    self.runtime.persist('audio_delivery',{'delivery':message.get('delivery') if message.get('delivery') in ('scheduled','ended','cleared','withheld') else 'unknown',
                        'direction_revision':self.runtime.session.direction_revision,'playback_verified':False})
                elif kind=='played':self.runtime.played(message.get('playback_id'),message.get('receipt',{}),message.get('complete') is True)
                elif kind=='interrupt':
                    if message.get('playback_id'):self.runtime.played(message['playback_id'],message.get('receipt',{}),False)
                    await self.runtime.interrupt(message.get('receipt',{}))
                elif kind=='close':
                    close_reason='client_close'
                    cost=await self.close()
                    if sender and not sender.done():
                        sender.cancel()
                        await asyncio.gather(sender,return_exceptions=True)
                    await ws.send_json({'type':'closed','cost':cost});break
                else:raise ValueError('不支援的串流訊息')
        except WebSocketDisconnect as error:
            close_reason=f'peer_disconnect_{error.code}'
        except (ValueError,TypeError,KeyError,TimeoutError,RuntimeError) as error:
            if close_reason=='unknown':close_reason=f'protocol_or_runtime_{type(error).__name__}'
            try:await ws.close(code=4400);socket_closed=True
            except RuntimeError:pass
        except Exception as error:
            report_failure('stream_'+str(last_kind),error)
            close_reason=f'unexpected_{type(error).__name__}'
            try:await ws.close(code=1011);socket_closed=True
            except RuntimeError:pass
        finally:
            receiving=[task for task in (incoming,stopping) if task]
            for task in receiving:
                if not task.done():task.cancel()
            if receiving:await asyncio.gather(*receiving,return_exceptions=True)
            if owns_connection:logger.info('connection_closed reason=%s last_kind=%s',close_reason,last_kind)
            if sender:
                sender.cancel()
                result=await asyncio.gather(sender,return_exceptions=True)
                if result and isinstance(result[0],Exception) and not isinstance(result[0],asyncio.CancelledError):
                    logger.info('sender_stopped type=%s',type(result[0]).__name__)
            if owns_connection:
                if self.stopped.is_set() and not socket_closed:
                    try:await ws.close(code=1013 if self.overflowed else 1000)
                    except RuntimeError:pass
                self.connected=False
                if self.closed:
                    if self.cost is None:await self.close()
                else:
                    # Device streams stop immediately; keep only the lesson state during a short retry window.
                    self.detaching=True
                    try:
                        self.runtime.set_mic(False)
                        if getattr(self.runtime.session,'basic_workflow',False):
                            self.runtime.set_camera(False,preserve_photo=True)
                        else:self.runtime.set_camera(False)
                        try:await self.runtime.interrupt({})
                        except Exception as error:logger.info('detach_interrupt_failed type=%s',type(error).__name__)
                        while not self.outbox.empty():self.outbox.get_nowait()
                        if not self.closed:
                            self.arm_expiry()
                            logger.info('reconnect_wait seconds=%s',self.reconnect_seconds)
                    finally:self.detaching=False
