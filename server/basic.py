"""Basic voice pipeline, independent from HTTP and the desktop renderer.

Media I/O is injected, so cancellation and stale-frame behavior can be tested
without microphones, API credentials, or a child's personal data.
"""
import asyncio
import base64
import binascii
import hashlib
import io
import logging
import time
import wave
import re
from datetime import datetime,timezone
from uuid import uuid4

import edge_tts
import httpx
from .tutor import TutorEngine, TutorSession
from .identity import EDGE_VOICE, DEFAULT_TEACHER_NAME, resolve_voice
from .lesson import apply_lesson,lesson_state
from .curriculum_map import teaching_references

logger=logging.getLogger('tutor.transport')

HALLUCINATION_PATTERNS = [
    r'^(?:thank\s+you|thanks|thanks\s+for\s+watching|thank\s+you\s+for\s+watching|thank\s+you\s+very\s+much|bye|you|subtitles?\s+by|please\s+subscribe)[.! ]*$',
    r'^(?:謝謝|謝謝大家|謝謝收看|謝謝觀看|感謝收看|感謝您的收看|感謝大家|請訂閱|歡迎訂閱|中文字幕|字幕志願者|字幕由|本集完|下集預告|響鐘|音樂|不吝點贊|請不吝點贊)[!！。，\s]*$',
    r'^使用者會稱呼老師叫.{1,30}老師[。.!！\s]*$',
]
HALLUCINATION_REGEX = [re.compile(p, re.IGNORECASE) for p in HALLUCINATION_PATTERNS]

def sanitize_transcript(text: str, segments: list | None = None) -> str:
    if not text: return ''
    cleaned = text.strip()
    if not cleaned: return ''
    if segments:
        probs = [s.get('no_speech_prob', 0) for s in segments if isinstance(s, dict)]
        if probs and (all(p > 0.55 for p in probs) or sum(probs) / len(probs) > 0.6):
            return ''
    for regex in HALLUCINATION_REGEX:
        if regex.match(cleaned):
            return ''
    stripped = re.sub(r'[\s.,!?。，！？、~～…\-_/]', '', cleaned)
    if not stripped: return ''
    if len(stripped) <= 20:
        lower = stripped.lower()
        if any(w in lower for w in [
            'thankyou', 'thanksforwatching', 'subtitlesby', 'pleasesubscribe',
            '中文字幕', '字幕志願者', '不吝點贊', '感謝收看', '謝謝收看', '謝謝觀看',
            '本集完', '下集預告', '訂閱', '点赞', '點贊', '投幣', '關注', '关注'
        ]):
            return ''
    return cleaned

class SpeechService:
    def __init__(self,groq_key,teacher_name='',voice=''):
        self.key=groq_key
        self.teacher_name=teacher_name or DEFAULT_TEACHER_NAME
        self.voice=resolve_voice(voice or EDGE_VOICE)
    async def transcribe(self,pcm,rate=16000):
        if not self.key:raise RuntimeError('尚未設定 Groq API 金鑰')
        if len(pcm)%2 or len(pcm)>rate*2*30:raise ValueError('錄音格式或長度不正確')
        output=io.BytesIO()
        with wave.open(output,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(rate);wav.writeframes(pcm)
        async with httpx.AsyncClient(timeout=30) as http:
            result=await http.post('https://api.groq.com/openai/v1/audio/transcriptions',headers={'Authorization':'Bearer '+self.key},data={'model':'whisper-large-v3','language':'zh','response_format':'verbose_json','temperature':'0.0'},files={'file':('utterance.wav',output.getvalue(),'audio/wav')})
        if not result.is_success:
            logger.info('transcription_http_failed status=%d',result.status_code)
            raise RuntimeError(f'Groq 轉錄失敗 ({result.status_code})')
        data=result.json()
        raw_text=data.get('text','').strip() if isinstance(data, dict) else str(data).strip()
        segments=data.get('segments',[]) if isinstance(data, dict) else []
        text=sanitize_transcript(raw_text, segments)
        probs=[s.get('no_speech_prob') for s in segments if isinstance(s,dict) and isinstance(s.get('no_speech_prob'),(int,float))]
        return {'text':text,'quality':{'raw_characters':len(raw_text),'segment_count':len(segments),
                'mean_no_speech_prob':round(sum(probs)/len(probs),3) if probs else None,
                'rejected':bool(raw_text and not text)}}
    async def synthesize(self,text):
        async def render():
            audio=io.BytesIO();boundaries=[]
            async for part in edge_tts.Communicate(text,self.voice,boundary='WordBoundary').stream():
                if part['type']=='audio':audio.write(part['data'])
                elif part['type']=='WordBoundary':boundaries.append({k:part[k] for k in ('text','offset','duration')})
            return {'audio':base64.b64encode(audio.getvalue()).decode(),'boundaries':boundaries,'text':text}
        return await asyncio.wait_for(render(),45)

class LatestObservation:
    def __init__(self,work,on_error):
        self.work=work;self.on_error=on_error;self.pending=None;self.task=None;self.closed=False;self.skipped=0
    def push(self,frame):
        if self.closed:return
        if self.pending is not None:self.skipped+=1
        self.pending=frame
        if self.task is None or self.task.done():self._start()
    def _start(self):
        self.task=asyncio.create_task(self.run())
        self.task.add_done_callback(self._resume_pending)
    def _resume_pending(self,task):
        if self.task is task and not self.closed and self.pending is not None:self._start()
    def cancel_current(self):
        self.pending=None
        if self.task and not self.task.done():self.task.cancel()
    async def run(self):
        while self.pending is not None and not self.closed:
            frame=self.pending;self.pending=None
            started=time.monotonic()
            logger.info('observation_begin')
            try:
                await self.work(frame)
                logger.info('observation_end elapsed_ms=%d',round((time.monotonic()-started)*1000))
            except asyncio.CancelledError:raise
            except Exception as error:
                logger.info('observation_failed type=%s elapsed_ms=%d',type(error).__name__,round((time.monotonic()-started)*1000))
                await self.on_error(str(error))
    async def close(self):
        self.closed=True;self.pending=None
        if self.task and not self.task.done():self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)

class BasicRuntime:
    def __init__(self,session:TutorSession,teacher,observer,speech,emit,persist,photo_workspace=None):
        self.session=session;self.teacher=TutorEngine(teacher)
        self.observer=self.teacher if observer is teacher else TutorEngine(observer)
        self.speech=speech;self.emit=emit;self.persist=persist;self.photo_workspace=photo_workspace
        self.audit=None
        self.photo_tasks=set()
        self.voice_lock=asyncio.Lock();self.voice_tasks=set();self.capture_waiters={}
        self.frame=None;self.camera_epoch=0;self.camera_enabled=True;self.mic_epoch=0;self.mic_enabled=True
        self.session.camera_enabled=self.camera_enabled
        self.board_revision=0;self.pending_playback={};self.groq_billable_seconds=0
        self.observations=LatestObservation(self.observe,self.error)
        self.begun=False;self.proactive_task=None;self.last_activity=time.monotonic()
        self.last_intervention=0;self.last_focus='';self.signal_focus='';self.signal_since=0;self.signal_count=0
        self.last_observation_version=0
        self.summary_saved=False;self.remembered_observations=set()
    async def error(self,message):
        if self.session.active:await self.emit({'type':'error','message':message or '老師的連線處理逾時，請再說一次，或用文字求助。'})
    async def send(self,event):
        if self.session.active:await self.emit({**event,'session_id':self.session.id,'generation':self.session.generation})
    def set_mic(self,enabled):
        self.mic_enabled=enabled;self.mic_epoch+=1
    def set_camera(self,enabled,preserve_photo=False):
        self.camera_enabled=enabled;self.camera_epoch+=1
        self.session.camera_enabled=enabled
        self.session.observation=None
        self.signal_count=0;self.signal_focus=''
        if not enabled:
            self.frame=None;self.observations.cancel_current()
            for waiter in self.capture_waiters.values():
                if not waiter.done():waiter.set_result(None)
    def accept_frame(self,image,request_id=None,capture_meta=None):
        if not self.session.active or not self.camera_enabled:return
        png_allowed = getattr(self, 'mode', None) == 'gpt' and not self.photo_workspace
        prefixes = ('data:image/jpeg;base64,', 'data:image/png;base64,') if png_allowed else ('data:image/jpeg;base64,',)
        limit = 16_000_000 if png_allowed else 3_000_000
        if not isinstance(image,str) or not image.startswith(prefixes) or len(image)>limit:raise ValueError('無效照片或照片過大；請框選作業範圍')
        try:decoded=base64.b64decode(image.split(',',1)[1],validate=True)
        except binascii.Error as error:raise ValueError('照片編碼無效') from error
        signature = b'\x89PNG\r\n\x1a\n' if image.startswith('data:image/png;') else b'\xff\xd8\xff'
        if not decoded.startswith(signature):raise ValueError('照片格式無效')
        if capture_meta is not None and not isinstance(capture_meta,dict):raise ValueError('無效拍攝資訊')
        meta={key:capture_meta[key] for key in ('source_width','source_height','corners','rotation','fine_rotation','full_frame','capture_method','captured_at')
              if isinstance(capture_meta,dict) and key in capture_meta}
        frame={'id':uuid4().hex,'image':image,'received':time.monotonic(),'camera_epoch':self.camera_epoch,
               'captured_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),'capture_meta':meta}
        if self.photo_workspace:
            async def publish_photo():
                notice=await self.photo_workspace.publish(image,frame['captured_at'],
                                                          capture_id=frame['id'],camera_epoch=frame['camera_epoch'])
                if self.audit:
                    self.audit.record('basic_photo_saved',capture_id=frame['id'],capture_meta=meta,
                                      captured_at=frame['captured_at'],archive_path=notice['archive_path'],
                                      sha256=hashlib.sha256(decoded).hexdigest())
                return {**notice,'capture_meta':meta}
            task=asyncio.create_task(publish_photo());frame['photo_task']=task
            self.photo_tasks.add(task);task.add_done_callback(self.photo_tasks.discard)
        elif self.audit:
            task=asyncio.create_task(asyncio.to_thread(
                self.audit.save_live_photo,frame['id'],image,frame['captured_at']))
            self.photo_tasks.add(task);task.add_done_callback(self.photo_tasks.discard)
        self.frame=frame
        if request_id:
            waiter=self.capture_waiters.get(request_id)
            if waiter and not waiter.done():waiter.set_result(frame)
        elif not self.voice_tasks:self.observations.push(frame)
    async def capture_now(self):
        if not self.camera_enabled:return None
        request_id=uuid4().hex;future=asyncio.get_running_loop().create_future();self.capture_waiters[request_id]=future
        try:
            await self.send({'type':'capture_requested','request_id':request_id})
            return await asyncio.wait_for(future,12)
        except TimeoutError:return None
        finally:self.capture_waiters.pop(request_id,None)
    async def observe(self,frame):
        if not self.camera_enabled or frame['camera_epoch']!=self.camera_epoch:return
        notice=await frame['photo_task'] if self.photo_workspace else None
        if not self.camera_enabled or frame['camera_epoch']!=self.camera_epoch:return
        result=await self.observer.respond(self.session,'請只記錄畫面中可見的作業內容與不確定性。',
            image_url=None if self.photo_workspace else frame['image'],observation=True,photo_notice=notice)
        if self.photo_workspace and notice['version']<=self.last_observation_version:return
        if result is None or not self.camera_enabled or frame['camera_epoch']!=self.camera_epoch or time.monotonic()-frame['received']>30:return
        if self.photo_workspace:self.last_observation_version=notice['version']
        self.session.observation={'evidence':result['evidence'],'received':time.monotonic(),
                                  'capture_id':frame['id'],'captured_at':frame.get('captured_at','')}
        self.persist('camera_observation',{'capture_id':frame['id'],'captured_at':frame.get('captured_at',''),
            'photo_version':notice['version'] if notice else None,
            'evidence':result['evidence'],'signal':result['lesson']['signal'],
            'focus':result['lesson']['focus'],'confidence':result['lesson']['confidence']})
        await self.send({'type':'observation','capture_id':frame['id'],'evidence':result['evidence'],'skipped':self.observations.skipped})
        signal=result['lesson'];focus=signal['signal']+':'+signal['focus']
        if signal['signal']=='none' or signal['confidence']!='high' or not signal['focus'] or not result['evidence']:
            self.signal_count=0;self.signal_focus='';return
        if focus!=self.signal_focus:
            self.signal_focus=focus;self.signal_since=time.monotonic();self.signal_count=1
        else:self.signal_count+=1
        wait=40 if signal['signal']=='repeated_correction' else 10
        if self.signal_count>=2 and time.monotonic()-self.signal_since>=wait:
            evidence_key=(signal['signal'],signal['focus'])
            if evidence_key not in self.remembered_observations:
                refs=teaching_references(self.session.grade,' '.join(result['evidence']),
                    self.session.notes,self.session.tasks)
                if refs:
                    ref=refs[0]
                    self.persist('learning_evidence',{
                        'code':ref['code'],'subject':ref['subject'],
                        'label':ref['summary'].split('：',1)[0][:120] or ref['code'],
                        'kind':'observed_step' if signal['signal']=='completed_step' else 'repeated_revision',
                        'quote':(signal['focus']+'：'+'；'.join(result['evidence']))[:500],
                        'origin':'camera_observation','source_page':ref['page']})
                    self.remembered_observations.add(evidence_key)
        if (self.signal_count>=2 and time.monotonic()-self.signal_since>=wait and focus!=self.last_focus
                and not self.voice_tasks and self.can_intervene(frame['camera_epoch'])):
            self.proactive_task=self.launch(self.handle_text('背景觀察發現可核對的變化：'+str(result['evidence'])+
                '。若確實是進展，簡短肯定具體步驟；若反覆修改，先問是否需要一點提示。證據不足就 quiet，不解出答案。',internal=True,proactive=True))
    def accept_audio(self,pcm,utterance_id=None,audio_meta=None):
        if not self.session.active or not self.mic_enabled:return False
        self.activity(student_turn=True)
        if len(self.voice_tasks)>=3:raise RuntimeError('老師正在處理前面的話，請稍候再說一次。')
        if self.audit:
            self.audit.record('audio_segment',utterance_id=utterance_id,audio_bytes=len(pcm),
                              capture=audio_meta if isinstance(audio_meta,dict) else {})
        task=asyncio.create_task(self.handle_audio(pcm,self.mic_epoch,utterance_id));self.voice_tasks.add(task)
        task.add_done_callback(self.voice_tasks.discard)
        return True
    async def handle_audio(self,pcm,mic_epoch,utterance_id=None):
        async with self.voice_lock:
            ticket=self.session.generation
            if not self.session.active or mic_epoch!=self.mic_epoch:return
            try:
                await self.send({'type':'audio_status','stage':'transcribing','utterance_id':utterance_id})
                started=time.monotonic()
                logger.info('transcription_begin audio_bytes=%d',len(pcm))
                transcription=await self.speech.transcribe(pcm)
                text=transcription.get('text','') if isinstance(transcription,dict) else transcription
                if self.audit and isinstance(transcription,dict):
                    self.audit.record('transcription_quality',utterance_id=utterance_id,
                                      audio_bytes=len(pcm),**transcription.get('quality',{}))
                logger.info('transcription_end elapsed_ms=%d has_text=%s',round((time.monotonic()-started)*1000),bool(text))
                self.groq_billable_seconds+=max(10,len(pcm)/32000)
                if not self.session.active or ticket!=self.session.generation or mic_epoch!=self.mic_epoch:return
                if text:
                    await self.send({'type':'audio_status','stage':'accepted','utterance_id':utterance_id})
                    await self.teach(text,ticket,utterance_id=utterance_id)
                else:await self.send({'type':'audio_status','stage':'ignored','utterance_id':utterance_id})
            except asyncio.CancelledError:raise
            except Exception as error:
                logger.info('audio_turn_failed type=%s',type(error).__name__)
                await self.send({'type':'audio_status','stage':'rejected','utterance_id':utterance_id})
                await self.error(str(error))

    def launch(self,work):
        task=asyncio.create_task(work);self.voice_tasks.add(task)
        task.add_done_callback(self.voice_tasks.discard)
        return task

    def begin(self):
        if not self.session.basic_workflow or self.begun:return
        self.begun=True
        self.launch(self.handle_text(f'開始陪讀：簡短介紹你是{self.session.teacher_name}，根據本次注意事項提出作業清單並確認；沒有範圍時先問今天要做哪些作業。',internal=True))

    def accept_text(self,text):
        if not isinstance(text,str) or not 1<=len(text.strip())<=4000:raise ValueError('請輸入 1–4000 字')
        if not self.session.active:return
        if len(self.voice_tasks)>=3:raise ValueError('請稍候老師回覆')
        self.activity(student_turn=True);self.launch(self.handle_text(text.strip()))

    async def handle_text(self,text,internal=False,proactive=False):
        async with self.voice_lock:
            if not self.session.active:return
            try:await self.teach(text,self.session.generation,internal,proactive)
            except asyncio.CancelledError:raise
            except Exception as error:
                logger.info('text_turn_failed type=%s',type(error).__name__)
                await self.error(str(error))

    async def teach(self,text,ticket,internal=False,proactive=False,utterance_id=None):
        if not internal:
            self.session.heard_history.append({'role':'user','text':text})
            self.persist('student_utterance',{'text':text,'source':'speech' if utterance_id else 'text'})
            await self.send({'type':'transcript','role':'user','text':text,'utterance_id':utterance_id})
        await self.send({'type':'thinking'})
        frame=self.frame if self.camera_enabled and self.frame and time.monotonic()-self.frame['received']<=20 else None
        camera_epoch=self.camera_epoch
        notice=await frame['photo_task'] if frame and self.photo_workspace else None
        if frame and (not self.camera_enabled or frame['camera_epoch']!=self.camera_epoch):
            frame=None;notice=None
        started=time.monotonic()
        logger.info('teacher_turn_begin kind=%s', 'proactive' if proactive else 'internal' if internal else 'student')
        result=await self.teacher.respond(self.session,text,
            image_url=frame['image'] if frame and not self.photo_workspace else None,
            image_age_seconds=time.monotonic()-frame['received'] if frame else None,
            photo_notice=notice,
            input_source='speech_transcript' if utterance_id else 'internal' if internal else 'typed_text')
        logger.info('teacher_turn_end elapsed_ms=%d has_result=%s',round((time.monotonic()-started)*1000),bool(result))
        if (not result or not self.session.active or ticket!=self.session.generation
                or (frame and frame['camera_epoch']!=self.camera_epoch)):return
        if result['need_capture'] and self.camera_enabled:
            fresh=await self.capture_now()
            if (fresh and self.session.active and ticket==self.session.generation
                    and self.camera_enabled and fresh['camera_epoch']==self.camera_epoch):
                fresh_notice=await fresh['photo_task'] if self.photo_workspace else None
                if self.camera_enabled and fresh['camera_epoch']==self.camera_epoch:
                    frame=fresh
                    result=await self.teacher.respond(self.session,text+'\n這是依要求取得的新照片；若仍看不清，請直接告知。',
                        image_url=None if self.photo_workspace else fresh['image'],
                        image_age_seconds=time.monotonic()-fresh['received'],photo_notice=fresh_notice,
                        input_source='speech_transcript' if utterance_id else 'internal' if internal else 'typed_text')
        if (not result or not self.session.active or ticket!=self.session.generation
                or (frame and frame['camera_epoch']!=self.camera_epoch)):return
        if proactive and not self.can_intervene(camera_epoch):return
        if result.get('curriculum_candidates'):
            self.persist('curriculum_candidates',{'items':result['curriculum_candidates']})
        if not internal and re.search(r'不會|不懂|怎麼|如何|為什麼|教我|提示|幫忙|求助',text):
            self.remember_help(result,text)
        if self.session.basic_workflow and not internal:
            if apply_lesson(self.session,result['lesson'],text):
                self.persist('lesson_state',lesson_state(self.session))
                await self.send({'type':'lesson',**lesson_state(self.session)})
        elif self.session.basic_workflow and internal and not proactive and result['lesson']['tasks']:
            # Opening proposal cannot confirm or complete work without the student.
            apply_lesson(self.session,result['lesson'],'')
            self.persist('lesson_state',lesson_state(self.session))
            await self.send({'type':'lesson',**lesson_state(self.session)})
        if result['board_html'] or result['close_board']:
            self.board_revision+=1
            await self.send({'type':'board','html':result['board_html'],'close':result['close_board'],'revision':self.board_revision})
        if not result['speech']:
            await self.send({'type':'idle'});return
        tts_started=time.monotonic()
        logger.info('speech_synthesis_begin')
        try:
            rendered=await self.speech.synthesize(result['speech'])
            logger.info('speech_synthesis_end elapsed_ms=%d',round((time.monotonic()-tts_started)*1000))
        except Exception:
            logger.info('speech_synthesis_failed elapsed_ms=%d',round((time.monotonic()-tts_started)*1000))
            if self.session.active and ticket==self.session.generation:
                await self.send({'type':'transcript','role':'assistant','text':result['speech'],'delivery':'text_only'})
                self.persist('text_reply',{'text':result['speech'],'spoken':False})
                await self.error('語音播放暫時無法準備，提示已顯示在對話紀錄。')
            return
        if not self.session.active or ticket!=self.session.generation:return
        if proactive and not self.can_intervene(camera_epoch):return
        playback_id=uuid4().hex;self.pending_playback[playback_id]=result['speech']
        self.session.last_speech=time.monotonic()
        if proactive:self.last_intervention=time.monotonic();self.last_focus=self.signal_focus
        await self.send({'type':'speech','playback_id':playback_id,**rendered})

    def remember_help(self,result,text,origin='student_utterance'):
        refs=result.get('curriculum_candidates') or []
        if not refs:return
        ref=refs[0]
        self.persist('learning_evidence',{
            'code':ref['code'],'subject':ref['subject'],'label':ref['label'],
            'kind':'asked_for_help','quote':text[:500],
            'origin':origin,'source_page':ref['page']})

    def activity(self,student_turn=False):
        self.last_activity=time.monotonic()
        if self.proactive_task and not self.proactive_task.done():self.proactive_task.cancel()
        if student_turn:self.observations.cancel_current()

    def can_intervene(self,camera_epoch):
        now=time.monotonic()
        return (self.session.active and self.camera_enabled and camera_epoch==self.camera_epoch
                and self.mic_enabled and not self.pending_playback and self.session.plan_confirmed
                and now-self.last_activity>=15 and now-self.session.last_speech>=60
                and now-self.last_intervention>=120)

    async def update_lesson(self,action,task_id=None):
        if not self.session.basic_workflow:return
        if action=='confirm' and self.session.tasks:self.session.plan_confirmed=True
        elif action in ('done','todo'):
            task=next((t for t in self.session.tasks if t['id']==task_id),None)
            if not task:raise ValueError('作業不存在')
            task['status']=action;task['evidence_quote']='學生在畫面上勾選' if action=='done' else ''
            self.session.manual_task_status[task_id]=action
        else:raise ValueError('無效作業操作')
        self.session.plan_revision+=1
        self.persist('lesson_state',lesson_state(self.session))
        await self.send({'type':'lesson',**lesson_state(self.session)})

    def played(self,playback_id,receipt,complete=False):
        text=self.pending_playback.pop(playback_id,None)
        if text is None or not self.session.active:return
        heard=text if complete else str(receipt.get('fully_heard',''))
        if heard:self.session.heard_history.append({'role':'assistant','text':heard})
        self.persist('playback',{'id':playback_id,'heard':heard,'complete':complete,'receipt':receipt if not complete else None})
    async def interrupt(self,receipt=None):
        if not self.session.active:return
        self.session.interrupt(receipt or {})
        # Invalidate before cancellation: even providers that ignore cancel cannot commit.
        for task in list(self.voice_tasks):task.cancel()
        await asyncio.gather(*self.voice_tasks,return_exceptions=True)
        self.pending_playback.clear()
        await self.send({'type':'interrupted'})
    async def close(self):
        self.session.end();self.mic_enabled=False;self.camera_enabled=False;self.frame=None
        for task in list(self.voice_tasks):task.cancel()
        for waiter in self.capture_waiters.values():
            if not waiter.done():waiter.cancel()
        await self.observations.close()
        await asyncio.gather(*self.voice_tasks,return_exceptions=True)
        if self.photo_tasks:await asyncio.gather(*self.photo_tasks,return_exceptions=True)
        if self.session.basic_workflow and not self.summary_saved:
            self.summary_saved=True
            self.persist('learning_summary',{'tasks':self.session.tasks,'confirmed':self.session.plan_confirmed,
                'student_questions':[x['text'] for x in self.session.heard_history if x['role']=='user'][-5:],
                'heard_guidance':[x['text'] for x in self.session.heard_history if x['role']=='assistant'][-5:],
                'basis':'僅整理已記錄作業與對話，不代表已掌握或已批改正確。'})
        self.pending_playback.clear()
        providers=[self.teacher.provider]
        if self.observer.provider is not providers[0]:providers.append(self.observer.provider)
        try:
            for provider in providers:
                if hasattr(provider,'close'):await provider.close()
        finally:
            if self.photo_workspace:await self.photo_workspace.close()
            if self.audit:await asyncio.to_thread(self.audit.snapshot_workspace)
        return {'groq_estimate_usd':self.groq_billable_seconds*.111/3600,'cli':'subscription','image_skipped':self.observations.skipped}
