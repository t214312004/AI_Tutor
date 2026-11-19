"""App-owned tools, photo context, and receipts for native voice classrooms."""

import asyncio
from collections import OrderedDict
import html
import time

from .classroom_runtime import ClassroomRuntime
from .curriculum_map import teaching_references, detect_subject
from .diagnostics import report_failure
from .lesson import LESSON_SCHEMA, apply_lesson, lesson_state, validate_lesson
from .photo_context import current_observation, photo_excerpt, photo_reference, read_photo_observation, stamp_observation
from .providers import ProviderError
from .tutor import validate_student_output, validate_visible_text
from .work_lifecycle import CURRENT_WORK
from .teaching_context import learning_context
from .teaching_cards import teaching_cards
from .learning_practice import ASSISTANCE, PHASES, ATTEMPT_FIELDS, validate_attempt


def tool(name, description, properties=None, required=None):
    return {'name': name, 'description': description,
            'inputSchema': {'type': 'object', 'additionalProperties': False,
                'properties': properties or {}, 'required': required or []}}


VERSION = {'context_version': {'type': 'string'}}
READ_ONLY_TOOLS = {'get_lesson_context', 'get_photo_observation'}
BACKGROUND_PHOTO_REUSE_SECONDS = 25
TOOLS = [
    tool('get_lesson_context', '取得正式課程、版本、最新照片索引、說話者及本次原話；修改前必須取得。'),
    tool('get_photo_observation', '依目前課堂提供的照片索引，讀取已提交的照片觀察短文。',
         {'ref': {'type': 'string'}}, ['ref']),
    tool('inspect_homework_photo', '請獨立看圖模型核對目前照片，只有文字回傳。',
         {'question': {'type': 'string'}, 'fresh': {'type': 'boolean'}}, ['question', 'fresh']),
    tool('update_whiteboard', '真的更新白板後才能向學生說更新完成；只寫學生要看的內容。',
         {**VERSION, 'capture_id': {'type': 'string'}, 'lines': {'type': 'array', 'items': {'type': 'string'}},
          'photo_ref': {'type': 'string', 'description': '使用照片觀察時填入該次核對的結果索引。純文字推理可省略，capture_id 留空。'},
          'html': {'type':'string','description':'選填；需要圖形或互動時使用隔離白板的內嵌 HTML/CSS/JS，不存取網路、檔案或父視窗。'},
          'close': {'type': 'boolean'}}, ['context_version', 'capture_id', 'lines', 'close']),
    tool('update_homework_plan', '提交畫面作業清單；完成與確認引用本次原話，不能以照片推定完成。',
         {**VERSION, 'lesson': LESSON_SCHEMA}, ['context_version', 'lesson']),
    tool('set_teaching_interaction', '依學生原話設定先聽完或恢復教學，不因沉默恢復。',
         {**VERSION, 'mode': {'type': 'string', 'enum': ['normal', 'listening', 'closing']},
          'quote': {'type': 'string'}}, ['context_version', 'mode', 'quote']),
    tool('record_learning_help', '保存本次明確求助的課綱證據；只用 context 的候選 code 和本次原話。',
         {**VERSION, 'code': {'type': 'string'}, 'quote': {'type': 'string'}, 'reason': {'type': 'string'}},
         ['context_version', 'code', 'quote', 'reason']),
    tool('record_learning_attempt', '家長啟用後，保存本次學生作答供家長核對；不自動判對錯或掌握。response 必須引用本次原話。',
         {**VERSION, **{key:{'type':'string'} for key in ATTEMPT_FIELDS},
          'assistance':{'type':'string','enum':list(ASSISTANCE)},
          'phase':{'type':'string','enum':list(PHASES)}}, ['context_version', *ATTEMPT_FIELDS]),
]


class LiveClassroomRuntime(ClassroomRuntime):
    def __init__(self, session, teacher, observer, emit, persist, photo_workspace=None):
        super().__init__(session, teacher, observer, emit, persist, photo_workspace)
        self.tool_lock = asyncio.Lock()
        self.receipts = OrderedDict()
        self.latest_user = ''
        self.photo_update_task = None
        self.photo_update_pending = False
        self.photo_updates_closed = False
        self.published_photo = None

    def defer_background_observation(self):
        # Independent vision may complete while the teacher is using its own session.
        return False

    def current_photo(self):
        return current_observation(self.session, self.frame, self.camera_epoch)

    def context(self, work=None, learning=None):
        observation = self.current_photo()
        text = work.request if work else self.latest_user
        refs = teaching_references(self.session.grade, text, self.session.notes, self.session.tasks,
            [observation.get('text','')] if observation else None)
        reviewed = self.session.reviewed_teaching_cards
        cards = teaching_cards(self.session.grade, text, allowed_ids=reviewed)
        if not cards and not detect_subject(text):
            cards = (teaching_cards(self.session.grade, ' '.join(r['summary'][:220] for r in refs), allowed_ids=reviewed)
                     or teaching_cards(self.session.grade, self.session.notes, allowed_ids=reviewed))
        practice_checks = (learning['practice_checks'] if learning is not None
                           else learning_context(self.session)['practice_checks'])
        return {'context_version': f'{self.session.generation}:{self.session.direction_revision}:{self.session.plan_revision}',
            'lesson': lesson_state(self.session), 'interaction_mode': self.session.interaction_mode,
            'speaker': work.speaker if work else self.session.input_speaker,
            'current_utterance': text, 'capture_id': observation['capture_id'] if observation else '',
            'photo': photo_reference(observation),
            'practice_checks': practice_checks,
            'teaching_cards': [{**card,'review':'parent_confirmed_for_this_student'} for card in cards],
            'curriculum_candidates': [{'code': r['code'], 'subject': r['subject'],
                'label': r['summary'][:120], 'page': r['page'],
                'source_title':r.get('source_title',''), 'source_url':r.get('source_url',''),
                'extraction':r.get('quality','automatic_unreviewed'),
                'basis':'自動擷取候選，未逐條人工核對；不是課本答案或實際進度。',
                'prerequisite':r.get('prerequisite',False)} for r in refs]}

    async def sync_context(self):
        """Adapters may refresh authoritative background without creating a teaching reply."""

    def prepare_observation(self, frame, observation):
        return stamp_observation(frame, observation)

    async def observe_natural(self, frame, *args, **kwargs):
        result = await super().observe_natural(frame, *args, **kwargs)
        if result and CURRENT_WORK.get() is None:
            self.queue_photo_update()
        return result

    def queue_photo_update(self):
        if self.photo_updates_closed or not self.session.active:
            return
        self.photo_update_pending = True
        if self.photo_update_task is None or self.photo_update_task.done():
            # Photo state belongs to the classroom, not the requesting turn.
            token = CURRENT_WORK.set(None)
            try:
                self.photo_update_task = asyncio.create_task(self.publish_photo_updates())
            finally:
                CURRENT_WORK.reset(token)

    async def publish_photo_updates(self):
        try:
            while self.photo_update_pending and self.session.active and not self.photo_updates_closed:
                self.photo_update_pending = False
                photo = photo_reference(self.current_photo())
                if photo != self.published_photo:
                    self.published_photo = photo
                    await self.send({'type': 'photo_observation_ready' if photo else 'photo_observation_withdrawn',
                                     'photo': photo})
                if not self.photo_update_pending:
                    await self.sync_photo_reference(photo)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            report_failure('publish_photo_updates', error)

    async def sync_photo_reference(self, photo):
        """Give the active voice adapter the current short reference, or its withdrawal."""

    def accept_frame(self, *args, **kwargs):
        accepted = super().accept_frame(*args, **kwargs)
        if accepted:
            self.queue_photo_update()
        return accepted

    def accept_background_frame(self, image, capture_meta):
        task = self.observations.task
        if self.directed_inspections or (task and not task.done()):
            return False
        photo = self.current_photo()
        if photo and time.monotonic() - photo['recognized_monotonic'] < BACKGROUND_PHOTO_REUSE_SECONDS:
            return False
        return self.accept_frame(image, capture_meta=capture_meta)

    def set_camera(self, enabled, preserve_photo=False):
        super().set_camera(enabled, preserve_photo)
        self.queue_photo_update()

    async def update_lesson(self, action, task_id=None):
        await super().update_lesson(action, task_id)
        self.queue_photo_update()

    async def close_photo_updates(self):
        self.photo_updates_closed = True
        if self.photo_update_task:
            self.photo_update_task.cancel()
            await asyncio.gather(self.photo_update_task, return_exceptions=True)

    async def close(self):
        if not self.photo_updates_closed:
            await self.close_photo_updates()
        return await super().close()

    async def dispatch_tool(self, key, work, name, args):
        """Apply a proposal after the adapter has acquired tool_lock and verified the turn."""
        _, turn_id, call_id = key
        cacheable = all(isinstance(part, str) and part for part in key)
        success, data = False, {'committed': False, 'reason': '操作已過期或欄位無效，請重新核對。'}
        read_only = isinstance(name, str) and name in READ_ONLY_TOOLS
        if (not self.photo_updates_closed and work and work.session is self.session
                and work.source_id == turn_id and work.valid()
                and cacheable and isinstance(name, str) and isinstance(args, dict)):
            if not read_only and key in self.receipts:
                cached_name, cached_success, cached_data = self.receipts[key]
                if name != cached_name:
                    return False, data
                if name == 'inspect_homework_photo' and cached_success:
                    inspected = cached_data.get('inspected_photo')
                    if inspected and inspected != photo_reference(self.current_photo()):
                        return False, data
                return cached_success, cached_data
            try:
                with work.bind():
                    data = await self.apply_tool(name, args, work)
                    success = True
            except (ValueError, KeyError, TypeError, TimeoutError, ProviderError):
                work.discard()
            if not read_only:
                self.receipts[key] = (name, success, data)
                while len(self.receipts) > 256:
                    self.receipts.popitem(last=False)
        self.persist('native_tool', {'tool': name, 'call_id': call_id, 'success': success,
            'generation': self.session.generation, 'plan_revision': self.session.plan_revision})
        return success, data

    async def apply_tool(self, name, args, work):
        definition = next((t for t in TOOLS if t['name'] == name), None)
        if (not definition or not set(definition['inputSchema']['required'])<=set(args)
                or not set(args)<=set(definition['inputSchema']['properties'])):
            raise ValueError('工具欄位無效')
        context = self.context(work)
        if name == 'get_lesson_context':
            return context
        if name == 'get_photo_observation':
            if not isinstance(args['ref'], str) or not 1 <= len(args['ref']) <= 140:
                raise ValueError('照片索引無效')
            result = read_photo_observation(self.current_photo(), args['ref'])
            if result['available']:
                work.photo_ref = result['photo']['ref']
            return result
        if name == 'inspect_homework_photo':
            if not isinstance(args['question'], str) or not 1 <= len(args['question']) <= 2000 or type(args['fresh']) is not bool:
                raise ValueError('照片請求無效')
            work.observation = None
            result = await self.inspect_photo(args['question'], args['fresh'])
            if not work.valid():
                raise ValueError('照片已過期')
            inspected = photo_reference(work.observation)
            if not inspected:
                work.discard()
                return {'committed': False, **photo_excerpt(result), **self.context(work), 'inspected_photo': None}
            if not work.commit():
                raise ValueError('照片已過期')
            work.photo_ref = inspected['ref']
            self.queue_photo_update()
            return {'committed': True, **photo_excerpt(result), **self.context(work), 'inspected_photo': inspected}
        if args['context_version'] != context['context_version']:
            raise ValueError('課程版本已改變')
        if name == 'update_whiteboard':
            lines = args['lines']
            markup=args.get('html','')
            capture_id = args['capture_id']
            photo_ref = args.get('photo_ref', '')
            if not isinstance(capture_id, str) or not isinstance(photo_ref, str):
                raise ValueError('照片索引無效')
            if (not isinstance(lines, list) or len(lines) > 16
                    or any(not isinstance(x, str) or not 1 <= len(x) <= 500 for x in lines)
                    or not isinstance(markup,str) or len(markup)>100_000
                    or type(args['close']) is not bool or not lines and not markup and not args['close']):
                raise ValueError('白板欄位無效')
            closing_only = args['close'] and not lines and not markup
            if not closing_only:
                if work.photo_ref and photo_ref != work.photo_ref:
                    raise ValueError('請提供本回合核對的照片索引')
                if capture_id or photo_ref:
                    current = context['photo']
                    if not current or capture_id != current['capture_id'] or photo_ref != current['ref']:
                        raise ValueError('照片結果已更新，請重新核對')
            validate_visible_text('\n'.join(lines))
            validate_student_output({'board_html':markup,'speech':''})
            if not work.commit():
                raise ValueError('白板已過期')
            self.board_revision += 1
            board = markup or '<main style="font-size:28px;line-height:1.7;padding:24px">' + ''.join('<p>' + html.escape(x) + '</p>' for x in lines) + '</main>'
            await self.send({'type': 'board', 'html': board, 'close': args['close'], 'revision': self.board_revision})
            self.persist('native_board', {'revision': self.board_revision, 'lines': lines, 'html':markup,
                'close': args['close'], 'capture_id': args['capture_id']})
        elif name == 'update_homework_plan':
            validate_lesson(args['lesson'])
            if not args['lesson']['tasks']:
                raise ValueError('作業清單不可空白')
            if not work.commit():
                raise ValueError('課程已過期')
            if apply_lesson(self.session, args['lesson'], work.request):
                self.session.plan_revision += 1
                work.plan_revision = self.session.plan_revision
                self.queue_photo_update()
                state = lesson_state(self.session)
                self.persist('lesson_state', state)
                await self.send({'type': 'lesson', **state})
        elif name == 'set_teaching_interaction':
            if (args['mode'] not in ('normal', 'listening', 'closing') or not isinstance(args['quote'], str)
                    or not args['quote'] or args['quote'] not in work.request):
                raise ValueError('對話節奏缺少本次原話')
            if not work.commit():
                raise ValueError('對話節奏已過期')
            await self.apply_interaction({'mode': args['mode'], 'reason': args['quote']}, work)
        elif name == 'record_learning_help':
            if any(not isinstance(args[k], str) or len(args[k]) > 500 for k in ('code', 'quote', 'reason')):
                raise ValueError('學習證據無效')
            if (not args['quote'].strip() or args['quote'] not in work.request or not args['reason'].strip()
                    or args['code'] not in {r['code'] for r in context['curriculum_candidates']}):
                raise ValueError('學習證據缺少本次原話或課綱來源')
            if not work.commit():
                raise ValueError('學習證據已過期')
            origin = 'provider_transcript' if work.source == 'native_voice' else 'student_utterance'
            self.remember_help({'curriculum_candidates': context['curriculum_candidates'],
                'learning': [{k: args[k] for k in ('code', 'quote', 'reason')}]}, work.request, origin=origin, work=work)
        elif name == 'record_learning_attempt':
            item = validate_attempt({key:args[key] for key in ATTEMPT_FIELDS})
            if (not self.session.practice_checks_enabled or work.speaker != 'student'
                    or work.source not in ('typed_text','native_voice')
                    or not item['response'] or item['response'] not in work.request):
                raise ValueError('練習紀錄缺少已啟用設定或可追溯的學生原話')
            if not work.valid():raise ValueError('練習紀錄已過期')
            self.persist('learning_attempt', {**item,
                'origin':'typed_text' if work.source=='typed_text' else 'provider_transcript',
                'source_id':getattr(work,'evidence_source_id',None) or work.source_id or work.id})
            if not work.commit():raise ValueError('練習紀錄已過期')
        if self.collaboration:
            self.collaboration.checkpoint(immediate=True)
        return {'committed': True, **self.context(work)}
