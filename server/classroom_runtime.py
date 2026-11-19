"""Classroom state, photo observations, and provider lifecycle shared by voice modes."""
import asyncio
import base64
import binascii
from datetime import datetime, timezone
import hashlib
import logging
import time
from uuid import uuid4

from .diagnostics import report_failure
from .lesson import lesson_state
from .tutor import TutorEngine, TutorSession
from .work_lifecycle import CURRENT_WORK

logger = logging.getLogger('tutor.transport')


def capture_timestamp(metadata, received_at):
    value = metadata.get('captured_at')
    if isinstance(value, str) and len(value) <= 40:
        try:
            captured_at = datetime.fromisoformat(value)
            if captured_at.tzinfo is not None:
                difference = (received_at - captured_at).total_seconds()
                if -5 <= difference <= 120:
                    return captured_at.astimezone(timezone.utc).isoformat(timespec='seconds'), 'device'
        except (ValueError, OverflowError):
            pass
    return received_at.isoformat(timespec='seconds'), 'received'


class LatestObservation:
    """Run one observation at a time, replacing the waiting frame as needed."""

    def __init__(self, work, on_error):
        self.work = work
        self.on_error = on_error
        self.pending = None
        self.task = None
        self.closed = False
        self.skipped = 0

    def push(self, frame):
        if self.closed:
            return
        if self.pending is not None:
            self.skipped += 1
        self.pending = frame
        if self.task is None or self.task.done():
            self._start()

    def _start(self):
        token = CURRENT_WORK.set(None)
        try:
            self.task = asyncio.create_task(self.run())
            self.task.add_done_callback(self._resume_pending)
        finally:
            CURRENT_WORK.reset(token)

    def _resume_pending(self, task):
        if self.task is task and not self.closed and self.pending is not None:
            self._start()

    def cancel_current(self):
        self.pending = None
        if self.task and not self.task.done():
            self.task.cancel()

    async def run(self):
        while self.pending is not None and not self.closed:
            frame, self.pending = self.pending, None
            started = time.monotonic()
            logger.info('observation_begin')
            try:
                await self.work(frame)
                logger.info('observation_end elapsed_ms=%d', round((time.monotonic() - started) * 1000))
            except asyncio.CancelledError:
                raise
            except TimeoutError as error:
                logger.info('observation_timeout type=%s', type(error).__name__)
            except Exception as error:
                report_failure('observation', error)
                try:
                    await self.on_error(str(error))
                except Exception as emit_error:
                    logger.info('observation_error_notify_failed type=%s', type(emit_error).__name__)

    async def close(self):
        self.closed = True
        self.pending = None
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class ClassroomRuntime:
    def __init__(self, session: TutorSession, teacher, observer, emit, persist, photo_workspace=None):
        self.session = session
        self.teacher = TutorEngine(teacher)
        self.observer = self.teacher if observer is teacher else TutorEngine(observer)
        self.emit = emit
        self.persist = persist
        self.photo_workspace = photo_workspace
        self.audit = None
        self.collaboration = session.collaboration
        self.photo_tasks = set()
        self.voice_lock = asyncio.Lock()
        self.voice_tasks = set()
        self.capture_waiters = {}
        self.frame = None
        self.camera_epoch = 0
        self.camera_enabled = True
        self.mic_epoch = 0
        self.mic_enabled = True
        self.directed_inspections = 0
        self.session.camera_enabled = self.camera_enabled
        self.board_revision = 0
        self.pending_playback = {}
        self.observations = LatestObservation(self.observe, self.error)
        self.begun = False
        self.summary_saved = False
        self.remembered_learning = set()
        if self.collaboration:
            self.collaboration.valid = self._observation_stamp
            self.collaboration.inspect = self.inspect_photo

    def _observation_stamp(self):
        return (self.session.active, self.session.generation, self.session.plan_revision, self.camera_epoch)

    async def error(self, message):
        if not self.session.active:
            return
        if self.collaboration:
            logger.info('lesson_processing_error')
            if self.audit:
                self.audit.record('lesson_processing_error', detail=message)
            message = '目前無法完成這次確認，請再說一次，或用文字告訴我。'
        await self.emit({'type': 'error', 'message': message or '老師的連線處理逾時，請再說一次，或用文字求助。'})

    async def send(self, event):
        work = CURRENT_WORK.get()
        if not self.session.active or (work and not work.valid()):
            return
        await self.emit({
            **event,
            'session_id': self.session.id,
            'generation': work.generation if work else self.session.generation,
            'work_sequence': work.sequence if work and work.committed else self.session.committed_sequence,
            'direction_revision': work.direction_revision if work else self.session.direction_revision,
        })

    def set_mic(self, enabled):
        self.mic_enabled = enabled
        self.mic_epoch += 1

    def set_camera(self, enabled, preserve_photo=False):
        self.camera_enabled = enabled
        self.camera_epoch += 1
        self.session.camera_enabled = enabled
        self.session.observation = None
        if self.collaboration:
            self.collaboration.clear_observation()
        if not enabled:
            self.frame = None
            self.observations.cancel_current()
            for waiter in self.capture_waiters.values():
                if not waiter.done():
                    waiter.set_result(None)

    def accepts_png(self):
        return getattr(self.observer.provider, 'vision_only', False)

    def accept_frame(self, image, request_id=None, capture_meta=None):
        if not self.session.active or not self.camera_enabled:
            return False
        waiter = self.capture_waiters.get(request_id) if request_id is not None else None
        if request_id is not None and (waiter is None or waiter.done()):
            return False
        png_allowed = self.accepts_png()
        prefixes = ('data:image/jpeg;base64,', 'data:image/png;base64,') if png_allowed else ('data:image/jpeg;base64,',)
        limit = 16_000_000 if png_allowed else 3_000_000
        if not isinstance(image, str) or not image.startswith(prefixes) or len(image) > limit:
            raise ValueError('無效照片或照片過大；請框選作業範圍')
        try:
            decoded = base64.b64decode(image.split(',', 1)[1], validate=True)
        except binascii.Error as error:
            raise ValueError('照片編碼無效') from error
        signature = b'\x89PNG\r\n\x1a\n' if image.startswith('data:image/png;') else b'\xff\xd8\xff'
        if not decoded.startswith(signature):
            raise ValueError('照片格式無效')
        if capture_meta is not None and not isinstance(capture_meta, dict):
            raise ValueError('無效拍攝資訊')
        metadata_keys = ('source_width', 'source_height', 'corners', 'rotation', 'fine_rotation',
                         'full_frame', 'capture_method', 'captured_at')
        meta = {key: capture_meta[key] for key in metadata_keys if capture_meta and key in capture_meta}
        received_at = datetime.now(timezone.utc)
        captured_at, capture_time_source = capture_timestamp(meta, received_at)
        frame = {
            'id': uuid4().hex,
            'image': image,
            'received': time.monotonic(),
            'camera_epoch': self.camera_epoch,
            'captured_at': captured_at,
            'received_at': received_at.isoformat(timespec='seconds'),
            'capture_time_source': capture_time_source,
            'capture_meta': meta,
        }
        if self.photo_workspace:
            async def publish_photo():
                try:
                    notice = await self.photo_workspace.publish(
                        image, frame['captured_at'], capture_id=frame['id'], camera_epoch=frame['camera_epoch'],
                    )
                    if self.audit:
                        self.audit.record(
                            'basic_photo_saved', capture_id=frame['id'], capture_meta=meta,
                            captured_at=frame['captured_at'], archive_path=notice['archive_path'],
                            sha256=hashlib.sha256(decoded).hexdigest(),
                        )
                    return {**notice, 'capture_meta': meta}
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    report_failure('publish_photo', error)
                    return None

            task = asyncio.create_task(publish_photo())
            frame['photo_task'] = task
            self.photo_tasks.add(task)
            task.add_done_callback(self.photo_tasks.discard)
        elif self.audit:
            task = asyncio.create_task(asyncio.to_thread(
                self.audit.save_live_photo, frame['id'], image, frame['captured_at'],
            ))
            self.photo_tasks.add(task)
            task.add_done_callback(self.photo_tasks.discard)
        self.frame = frame
        if self.collaboration:
            self.collaboration.latest_capture_id = frame['id']
        if waiter is not None:
            waiter.set_result(frame)
        elif not self.directed_inspections and not self.defer_background_observation():
            self.observations.push(frame)
        return True

    def defer_background_observation(self):
        return bool(self.voice_tasks)

    async def capture_now(self):
        if not self.camera_enabled:
            return None
        request_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.capture_waiters[request_id] = future
        try:
            await self.send({'type': 'capture_requested', 'request_id': request_id})
            # The desktop may spend up to 20 seconds in native capture and
            # 15 more seconds sending the image over the separate HTTP route.
            return await asyncio.wait_for(future, 40)
        except TimeoutError:
            return None
        finally:
            self.capture_waiters.pop(request_id, None)

    async def observe(self, frame):
        return await self.observe_natural(frame)

    async def observe_natural(self,frame,request='請記錄可見題目、學生字跡、不確定處及重要變化。',selected=False):
        stamp=self._observation_stamp()
        work=CURRENT_WORK.get()
        deadline=min(frame['received']+120,work.deadline if work else float('inf'))
        def current():
            return (self.session.active and self.camera_enabled
                    and stamp==self._observation_stamp()
                    and frame['camera_epoch']==self.camera_epoch
                    and (work is None or work.valid()))
        if not current() or time.monotonic()>=deadline:return None
        async with self.observer.lock:
            provider=self.observer.provider
            # File-aware CLIs use the immutable archive; APIs receive the original image.
            uses_files=hasattr(provider,'workspace') and not getattr(provider,'vision_only',False)
            notice=await frame['photo_task'] if (self.photo_workspace and uses_files) else None
            if not current():return None
            prompt=request+'\n照片：'+frame['id']+'\n拍攝時間：'+frame.get('captured_at','')
            if notice and uses_files:
                prompt+='\nselected_photo.archive_path：'+notice['archive_path']
                prompt+='\n照片絕對位置：'+str((self.photo_workspace.root/notice['archive_path']).resolve())
            remaining=deadline-time.monotonic()
            if remaining<=0:return None
            try:
                raw=await asyncio.wait_for(provider.turn(prompt,image_url=None if uses_files else frame['image'],
                                        schema=None,valid=current,timeout=remaining),remaining)
            except asyncio.CancelledError:
                if not current():return None
                raise
            if not isinstance(raw,str) or not 1<=len(raw.strip())<=12000:
                raise ValueError('照片觀察無法使用')
            if not current():return None
            observation={'capture_id':frame['id'],'captured_at':frame.get('captured_at',''),
                'received':time.monotonic() if selected else frame['received'],
                'captured_received':frame['received'],'historical':time.monotonic()-frame['received']>45,
                'generation':self.session.generation,'plan_revision':self.session.plan_revision,
                'text':raw,'evidence':[raw[:500]]}
            observation = self.prepare_observation(frame, observation)
            if self.collaboration and not self.collaboration.record_observation(frame, raw, stamp, allow_historical=True):
                return None
            payload={'capture_id':frame['id'],
                'captured_at':frame.get('captured_at',''),'observation_text':raw,
                'historical':observation['historical'],
                'evidence':[raw[:500]],'signal':'none','focus':'','confidence':'low'}
            for key in ('observation_id', 'recognized_at', 'received_at', 'capture_time_source'):
                if key in observation:
                    payload[key] = observation[key]
            if work:
                if not work.valid():return None
                work.observation=observation
                previous=self.session.observation
                work.stage((id(self),'session-observation'),lambda:setattr(self.session,'observation',observation),
                    undo=lambda:setattr(self.session,'observation',previous))
                work.stage((id(self),'observation-event',frame['id']),lambda:self.persist('camera_observation',payload))
            else:
                self.persist('camera_observation',payload)
                self.session.observation=observation
            self.after_observation(frame, observation)
            return raw

    def prepare_observation(self, frame, observation):
        """Add runtime metadata before the result is staged or committed."""
        return observation

    def after_observation(self, frame, observation):
        """Observe an accepted result, which may still belong to a staged request."""

    async def inspect_photo(self,request,fresh=False):
        stamp=self._observation_stamp()
        frame=await self.capture_now() if fresh else self.frame
        if stamp!=self._observation_stamp():return '課堂已改變，這次照片核對已取消。'
        if not self.camera_enabled:
            return '目前鏡頭已關閉，無法核對照片。請先向學生確認題目。'
        if not frame or frame['camera_epoch']!=self.camera_epoch:
            return '目前沒有可用的作業照片。請引導學生靠近鏡頭出示作業，或唸出題目。'
        if time.monotonic()-frame['received']>120:
            return '目前最新照片拍攝時間已超過時限，可能與當前進度不符。請向學生確認或請孩子提供清晰作業畫面。'
        self.observations.cancel_current()
        # A directed question refers to one immutable recent capture. Routine
        # camera ticks must not continuously supersede that question's evidence.
        self.directed_inspections+=1
        try:
            try:result=await self.observe_natural(frame,request,selected=True)
            except TimeoutError:return '這次照片核對未能在有效時間內完成，請依已確認資料或向學生釐清，不猜測。'
            if stamp!=self._observation_stamp():return '課堂已改變，這次照片核對已取消。'
            if not result:
                return '視覺夥伴仍在辨識此照片，請先陪伴孩子讀題或確認想法，切勿空等或猜測答案；核對結果會留存於紀錄。'
            context='核對照片：'+frame['id']+'；拍攝時間：'+frame.get('captured_at','')+'。這是拍攝當時的證據。'
            if self.frame and self.frame['id']!=frame['id']:
                context+='另有新照片待辨識，不能把本次核對描述成即時畫面。'
            return context+'\n'+result
        finally:
            self.directed_inspections-=1
            if (not self.directed_inspections and self.session.active and self.camera_enabled
                    and self.frame and self.frame['id']!=frame['id']):
                self.observations.push(self.frame)

    def launch(self,work):
        task=asyncio.create_task(work);self.voice_tasks.add(task)
        task.add_done_callback(self.voice_tasks.discard)
        return task

    def remember_help(self,result,text,origin='student_utterance',work=None):
        refs={r['code']:r for r in result.get('curriculum_candidates',[])}
        for proposal in result.get('learning',[]):
            quote=proposal['quote'].strip();ref=refs.get(proposal['code'])
            if not quote or quote not in text or not ref or not proposal['reason'].strip():continue
            candidate={'code':ref['code'],'subject':ref['subject'],'label':ref['label'],
                'kind':'asked_for_help','quote':quote,'origin':origin,'source_page':ref['page'],
                'reason':proposal['reason'],'request_id':work.id if work else None,
                'source_id':getattr(work,'evidence_source_id',work.source_id) if work else None,'speaker':work.speaker if work else self.session.input_speaker}
            self.persist('learning_candidate',candidate)
            key=(candidate['source_id'] or candidate['request_id'],candidate['code'],quote)
            if candidate['speaker']=='student' and key not in self.remembered_learning:
                self.persist('learning_evidence',candidate);self.remembered_learning.add(key)

    async def apply_interaction(self,proposal,work=None):
        mode=proposal.get('mode','unchanged')
        if mode=='unchanged':return
        if mode not in ('normal','listening','closing'):raise ValueError('無效對話節奏')
        reason=proposal.get('reason','')[:500]
        if mode!=self.session.interaction_mode:
            self.session.direction_revision+=1
        self.session.interaction_mode=mode;self.session.interaction_note=reason
        if work:work.rebase_direction()
        if mode!='normal':self.pending_playback.clear()
        if self.collaboration:self.collaboration.checkpoint(immediate=True)
        self.persist('interaction_policy',{'mode':mode,'reason':reason,'direction_revision':self.session.direction_revision})
        await self.send({'type':'interaction_policy','mode':mode,'reason':reason})

    async def interaction_control(self,mode,speaker=None):
        if mode not in ('unchanged','normal','listening','closing'):raise ValueError('無效對話節奏')
        if speaker is not None and speaker not in ('unknown','student','parent','onsite_teacher'):
            raise ValueError('無效說話者')
        speaker_changed = False
        if speaker is not None:
            if speaker!=self.session.input_speaker:
                self.session.direction_revision+=1
                speaker_changed = True
            self.session.input_speaker=speaker
            self.persist('speaker_context',{'speaker':speaker})
        if mode=='unchanged':
            if speaker_changed and self.collaboration:
                self.collaboration.checkpoint(immediate=True)
            return
        await self.apply_interaction({'mode':mode,'reason':'使用者在畫面上設定對話節奏'})

    def activity(self, student_turn=False):
        if student_turn and not self.collaboration:
            self.observations.cancel_current()

    async def update_lesson(self,action,task_id=None):
        if action=='confirm' and self.session.tasks:self.session.plan_confirmed=True
        elif action in ('done','todo'):
            task=next((t for t in self.session.tasks if t['id']==task_id),None)
            if not task:raise ValueError('作業不存在')
            task['status']=action;task['evidence_quote']='學生在畫面上勾選' if action=='done' else ''
            self.session.manual_task_status[task_id]=action
        else:raise ValueError('無效作業操作')
        self.session.plan_revision+=1
        if self.collaboration:self.collaboration.checkpoint()
        self.persist('lesson_state',lesson_state(self.session))
        await self.send({'type':'lesson',**lesson_state(self.session)})

    def played(self,playback_id,receipt,complete=False):
        text=self.pending_playback.pop(playback_id,None)
        if text is None or not self.session.active:return
        heard=text if complete else str(receipt.get('fully_heard',''))
        if heard:
            self.session.heard_history.append({'role':'assistant','text':heard})
            self.session.heard_history=self.session.heard_history[-60:]
        if self.collaboration:self.collaboration.checkpoint()
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
        providers=[self.teacher.provider]
        if self.observer.provider is not providers[0]:providers.append(self.observer.provider)
        try:
            if self.collaboration:self.collaboration.checkpoint();await self.collaboration.close()
        finally:
            # Storage failures must still stop media and credential-bearing CLIs.
            self.session.end();self.mic_enabled=False;self.camera_enabled=False;self.frame=None
            for task in list(self.voice_tasks):task.cancel()
            for waiter in self.capture_waiters.values():
                if not waiter.done():waiter.cancel()
            self.pending_playback.clear()
            try:
                await self.observations.close()
                await asyncio.gather(*self.voice_tasks,return_exceptions=True)
                if self.photo_tasks:await asyncio.gather(*self.photo_tasks,return_exceptions=True)
                if self.session.basic_workflow and not self.summary_saved:
                    self.summary_saved=True
                    self.persist('learning_summary',{'tasks':self.session.tasks,'confirmed':self.session.plan_confirmed,
                        'student_questions':[x['text'] for x in self.session.heard_history if x['role']=='user'][-5:],
                        'heard_guidance':[x['text'] for x in self.session.heard_history if x['role']=='assistant'][-5:],
                        'basis':'僅整理已記錄作業與對話，不代表已掌握或已批改正確。'})
            finally:
                results=await asyncio.gather(*(provider.close() for provider in providers if hasattr(provider,'close')),
                                             return_exceptions=True)
                try:
                    if self.photo_workspace:await self.photo_workspace.close()
                finally:
                    if self.audit:await asyncio.to_thread(self.audit.snapshot_workspace)
                for result in results:
                    if isinstance(result,BaseException):raise result
        return {'cli':'subscription',
                'image_skipped':self.observations.skipped}
