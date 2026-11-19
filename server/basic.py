"""Basic voice pipeline, independent from HTTP and the desktop renderer.

Media I/O is injected, so cancellation and stale-frame behavior can be tested
without microphones, API credentials, or a child's personal data.
"""
import asyncio
import logging
import time
from .work_lifecycle import LessonWork, CURRENT_WORK
from .diagnostics import report_failure
import re
from uuid import uuid4

from .classroom_runtime import ClassroomRuntime
from .tutor import TutorSession
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

class BasicRuntime(ClassroomRuntime):
    def __init__(self, session: TutorSession, teacher, observer, speech, emit, persist, photo_workspace=None):
        super().__init__(session, teacher, observer, emit, persist, photo_workspace)
        self.speech = speech
        if self.collaboration and speech is not None:
            self.collaboration.waiting = self.say_while_waiting
        self.groq_billable_seconds = 0
        self.proactive_task = None
        self.last_activity = time.monotonic()
        self.last_intervention = 0
        self.last_focus = ''
        self.signal_focus = ''
        self.signal_since = 0
        self.signal_count = 0
        self.last_observation_version = 0
        self.last_background_review = 0
        self.remembered_observations = set()

    def set_camera(self, enabled, preserve_photo=False):
        super().set_camera(enabled, preserve_photo)
        self.signal_count = 0
        self.signal_focus = ''

    def accepts_png(self):
        return getattr(self, 'mode', None) == 'gpt' or super().accepts_png()

    async def observe(self,frame):
        if self.collaboration:
            return await self.observe_natural(frame)
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
                    self.persist('learning_candidate',{
                        'code':ref['code'],'subject':ref['subject'],
                        'label':ref['summary'].split('：',1)[0][:120] or ref['code'],
                        'kind':'observed_step' if signal['signal']=='completed_step' else 'repeated_revision',
                        'quote':(signal['focus']+'：'+'；'.join(result['evidence']))[:500],
                        'origin':'camera_observation','source_page':ref['page'],
                        'capture_id':frame['id'],'reason':'視覺觀察提議，尚未由教學端確認概念與歸屬'})
                    self.remembered_observations.add(evidence_key)
        if (self.signal_count>=2 and time.monotonic()-self.signal_since>=wait and focus!=self.last_focus
                and not self.voice_tasks and self.can_intervene(frame['camera_epoch'])):
            self.proactive_task=self.launch(self.handle_text('背景觀察發現可核對的變化：'+str(result['evidence'])+
                '。若確實是進展，簡短肯定具體步驟；若反覆修改，先問是否需要一點提示。證據不足就 quiet，不解出答案。',internal=True,proactive=True))

    def after_observation(self, frame, observation):
        # Internal observation updates never appear in student captions or speech.
        if (not self.voice_tasks and self.can_intervene(frame['camera_epoch']) and
                time.monotonic()-self.last_background_review>=120 and self.session.basic_workflow):
            self.last_background_review=time.monotonic()
            self.proactive_task=self.launch(self.handle_text('參考已提交的照片觀察，判斷是否有值得輕聲提醒的具體進展或疑問。'
                '沒有可核對的新變化就 quiet；不因沉默催促，不朗讀觀察，不直接解答。',internal=True,proactive=True))

    async def say_while_waiting(self,text,stamp):
        work=CURRENT_WORK.get()
        def valid():
            return (self.session.active and self.collaboration.valid()==stamp
                    and self.session.interaction_mode=='normal' and (work is None or work.valid()))
        if not valid():return
        from .tutor import validate_visible_text
        validate_visible_text(text)
        try:rendered=await self.speech.synthesize(text)
        except asyncio.CancelledError:raise
        except Exception:
            if valid():await self.send({'type':'transcript','role':'assistant','text':text,'delivery':'text_only'})
            return
        if not valid():return
        playback_id=uuid4().hex
        self.pending_playback[playback_id]=text
        self.session.last_speech=time.monotonic()
        await self.send({'type':'speech','playback_id':playback_id,**rendered})

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
                report_failure('audio_turn',error)
                await self.send({'type':'audio_status','stage':'rejected','utterance_id':utterance_id})
                await self.error(str(error))

    def begin(self):
        if not self.session.basic_workflow or self.begun:return
        self.begun=True
        request=('接續剛才中斷的陪讀：依已提交筆記、作業與實際對話繼續，不重新自我介紹；舊照片已失效，有需要才確認新照片。'
                 if getattr(self.session,'resumed',False) else
                 f'開始陪讀：簡短介紹你是{self.session.teacher_name}，根據本次注意事項提出作業清單並確認；沒有範圍時先問今天要做哪些作業。')
        self.launch(self.handle_text(request,internal=True))

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
                report_failure('text_turn',error)
                await self.error(str(error))

    async def teach(self,text,ticket,internal=False,proactive=False,utterance_id=None):
        work=LessonWork(self.session,text,'internal' if internal else 'student_utterance',utterance_id)
        self.persist('request_lifecycle',{'request_id':work.id,'sequence':work.sequence,'stage':'started',
            'source':work.source,'source_id':work.source_id,'generation':work.generation,
            'plan_revision':work.plan_revision,'direction_revision':work.direction_revision})
        try:
            with work.bind():
                await self._teach(text,ticket,internal,proactive,utterance_id,work)
        finally:
            self.persist('request_lifecycle',{'request_id':work.id,'sequence':work.sequence,
                'stage':'committed' if work.committed else 'discarded'})
            work.discard()
            if (self.session.active and work.generation==self.session.generation and
                    work.sequence>=self.session.committed_sequence and not work.valid()):
                if utterance_id:await self.send({'type':'audio_status','stage':'ignored','utterance_id':utterance_id})
                else:await self.send({'type':'idle'})

    async def _teach(self,text,ticket,internal,proactive,utterance_id,work):
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
            image_url=frame['image'] if frame and not self.photo_workspace and not self.collaboration else None,
            image_age_seconds=time.monotonic()-frame['received'] if frame else None,
            photo_notice=None if self.collaboration else notice,
            input_source='speech_transcript' if utterance_id else 'internal' if internal else 'typed_text')
        logger.info('teacher_turn_end elapsed_ms=%d has_result=%s',round((time.monotonic()-started)*1000),bool(result))
        if (not result or not work.valid() or ticket!=self.session.generation
                or (frame and frame['camera_epoch']!=self.camera_epoch)):return
        if result['need_capture'] and self.camera_enabled:
            fresh=await self.capture_now()
            if (fresh and work.valid() and ticket==self.session.generation
                    and self.camera_enabled and fresh['camera_epoch']==self.camera_epoch):
                fresh_notice=await fresh['photo_task'] if self.photo_workspace else None
                if work.valid() and ticket==self.session.generation and self.camera_enabled and fresh['camera_epoch']==self.camera_epoch:
                    frame=fresh
                    if self.collaboration:await self.observe_natural(fresh,'請核對老師要求重拍的題目與作答。',selected=True)
                    result=await self.teacher.respond(self.session,text+'\n這是依要求取得的新照片；若仍看不清，請直接告知。',
                        image_url=None if (self.photo_workspace or self.collaboration) else fresh['image'],
                        image_age_seconds=time.monotonic()-fresh['received'],photo_notice=None if self.collaboration else fresh_notice,
                        input_source='speech_transcript' if utterance_id else 'internal' if internal else 'typed_text')
        if (not result or not work.valid() or ticket!=self.session.generation
                or (frame and frame['camera_epoch']!=self.camera_epoch)):return
        if proactive and not self.can_intervene(camera_epoch):return
        from .tutor import validate_student_output
        validate_student_output(result)
        if not work.commit():return
        if result.get('curriculum_candidates'):
            self.persist('curriculum_candidates',{'items':result['curriculum_candidates']})
        if not internal:self.remember_help(result,text,work=work)
        await self.apply_interaction(result.get('interaction',{}),work=work)
        if not work.valid():return
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
        if self.collaboration:self.collaboration.checkpoint()
        if not result['speech'] or self.session.interaction_mode!='normal':
            await self.send({'type':'idle'});return
        tts_started=time.monotonic()
        logger.info('speech_synthesis_begin')
        try:
            rendered=await self.speech.synthesize(result['speech'])
            logger.info('speech_synthesis_end elapsed_ms=%d',round((time.monotonic()-tts_started)*1000))
        except Exception:
            logger.info('speech_synthesis_failed elapsed_ms=%d',round((time.monotonic()-tts_started)*1000))
            if work.valid() and ticket==self.session.generation:
                await self.send({'type':'transcript','role':'assistant','text':result['speech'],'delivery':'text_only'})
                self.persist('text_reply',{'text':result['speech'],'spoken':False})
                await self.error('語音播放暫時無法準備，提示已顯示在對話紀錄。')
            return
        if not work.valid() or ticket!=self.session.generation or self.session.interaction_mode!='normal':return
        if proactive and not self.can_intervene(camera_epoch):return
        playback_id=uuid4().hex;self.pending_playback[playback_id]=result['speech']
        self.session.last_speech=time.monotonic()
        if proactive:self.last_intervention=time.monotonic();self.last_focus=self.signal_focus
        await self.send({'type':'speech','playback_id':playback_id,**rendered})

    def activity(self, student_turn=False):
        self.last_activity = time.monotonic()
        if self.proactive_task and not self.proactive_task.done():
            self.proactive_task.cancel()
        super().activity(student_turn)

    def can_intervene(self,camera_epoch):
        now=time.monotonic()
        return (self.session.active and self.camera_enabled and camera_epoch==self.camera_epoch
                and self.mic_enabled and not self.pending_playback and self.session.plan_confirmed
                and self.session.interaction_mode=='normal'
                and now-self.last_activity>=15 and now-self.session.last_speech>=60
                and now-self.last_intervention>=120)

    async def close(self):
        result = await super().close()
        return {'groq_estimate_usd': self.groq_billable_seconds * .111 / 3600, **result}
