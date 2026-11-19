import asyncio
import json
import logging
import os
import re
import shutil
import socket
import sys
import time
from datetime import datetime, timezone
from typing import Literal
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from .storage import Store
from .curriculum import coverage, search
from .curriculum_map import concepts, catalog_status
from .transport import RuntimeBridge
from .identity import student_profile
from .audit import SessionAudit,prune_audits,prune_provider_logs
from .paths import data_dir
from .diagnostics import report_failure
from .codex_voice import NATIVE_VOICES
from .live_classroom import LiveClassroomRuntime

ROOT = data_dir()
TOKEN = os.environ.get('TUTOR_CORE_TOKEN', '')
store = None
active = None
READY_PORT = None
_uvicorn_server = None
diagnostics=logging.getLogger('tutor.transport')
LOG_RETENTION_SECONDS=72*60*60

class ThreeDayRotatingHandler(RotatingFileHandler):
    def __init__(self,filename):
        path = Path(filename)
        if path.is_symlink():raise OSError('Connection log path is a link')
        super().__init__(filename,maxBytes=1_000_000,backupCount=2,encoding='utf-8')
        self.opened_at=self._initial_opened_at(path)
    @staticmethod
    def _initial_opened_at(path: Path):
        try:
            if path.is_file() and path.stat().st_size > 0:
                with path.open('r', encoding='utf-8', errors='replace') as stream:
                    first = stream.readline()
                if len(first) >= 19:
                    try:
                        return datetime.fromisoformat(first[:19]).replace(tzinfo=timezone.utc).timestamp()
                    except ValueError:
                        pass
                return path.stat().st_mtime
        except OSError:
            pass
        return time.time()
    def shouldRollover(self,record):
        return time.time()-self.opened_at>=24*60*60 or super().shouldRollover(record)
    def doRollover(self):
        super().doRollover()
        self.opened_at=time.time()

def prune_connection_logs(include_current=False):
    base=ROOT/'connection.log'
    cutoff=time.time()-LOG_RETENTION_SECONDS
    try:leftovers=list(ROOT.glob('connection.log.*.pruning'))
    except OSError:leftovers=[]
    for leftover in leftovers:
        try:
            if (re.fullmatch(r'connection\.log(?:\.[12])?\.[0-9a-f]{32}\.pruning',leftover.name)
                    and not leftover.is_symlink() and leftover.is_file() and leftover.stat().st_mtime<=cutoff):
                leftover.unlink()
        except OSError:pass
    for suffix in (('', '.1', '.2') if include_current else ('.1', '.2')):
        path=Path(str(base)+suffix)
        try:
            if path.is_symlink() or not path.is_file():continue
            fallback=path.stat().st_mtime
            lines=path.read_text(encoding='utf-8',errors='replace').splitlines(keepends=True)
            kept=[]
            last_created=fallback
            for line in lines:
                try:
                    created=datetime.fromisoformat(line[:19]).replace(tzinfo=timezone.utc).timestamp()
                    last_created=created
                except ValueError:
                    created=last_created
                if created>cutoff:kept.append(line)
            if len(kept)==len(lines):continue
            if not kept:path.unlink()
            else:
                replacement=path.with_name(path.name+'.'+uuid4().hex+'.pruning')
                try:
                    with replacement.open('x',encoding='utf-8') as stream:stream.write(''.join(kept))
                    replacement.replace(path)
                finally:replacement.unlink(missing_ok=True)
        except OSError:
            diagnostics.warning('connection_log_prune_failed file=%s',path.name)

async def create_runtime(state):
    from .collaboration import CollaborationWorkspace
    from .tutor import TutorSession
    profile = next(s for s in store.students() if s['id'] == state['student_id'])
    session = TutorSession(state['student_id'], profile['grade'], profile['preferences'] + '\n' + state['notes'],
                           id=state['id'], basic_workflow=True)
    session.student_name = profile['name']
    session.teacher_name = state['teacher_name']
    session.resumed = state['resumed']
    session.memory = store.learning_context(state['student_id'])
    session.long_term_memory = store.learning_profile(state['student_id'])
    settings = store.practice_settings(state['student_id'])
    session.practice_checks_enabled = settings['enabled']
    session.reviewed_teaching_cards = settings['reviewed_cards']
    session.learning_attempts = store.learning_attempts(state['student_id'], context_only=True)
    collaboration = CollaborationWorkspace(ROOT / 'runtime' / state['student_id'], session, 'basic')
    session.collaboration = collaboration
    if session.resumed:
        collaboration.restore()
    audit = audits.get(state['id'])
    if audit:
        audit.capture_guide()

    async def emit(event):
        pass

    def persist(kind, payload):
        if kind == 'native_voice_selection':
            state['codex_voice'] = payload['voice']
            if active and active['id'] == state['id']:
                active['codex_voice'] = payload['voice']
            config = {key: state[key] for key in ('mode', 'backend', 'teacher_name', 'basic_voice', 'codex_voice')}
            store.event(state['student_id'], state['id'], 'session_config', config)
        if kind == 'learning_attempt':
            from .learning_practice import ATTEMPT_FIELDS
            store.record_learning_attempt(state['student_id'], state['id'],
                {key:payload[key] for key in ATTEMPT_FIELDS}, payload['origin'], payload['source_id'])
        elif kind == 'learning_evidence':
            store.record_learning_evidence(state['student_id'], state['id'], payload)
        else:
            store.event(state['student_id'], state['id'], kind, payload)
        if audit:
            audit.record('lesson_event', event_kind=kind, payload=payload)

    observer = runtime = None
    try:
        if state['basic_voice'] == 'agy':
            from .agy_runtime import AgyLiveRuntime
            from .agy_vision import AgyAcpVision
            observer = AgyAcpVision()
            runtime = AgyLiveRuntime(session, observer, emit, persist, collaboration.photos)
            await runtime.ensure_ready()
        else:
            from .codex_voice import SubscriptionCodex
            from .native_basic import NativeBasicRuntime
            observer = SubscriptionCodex(collaboration.role_path('vision'))
            runtime = NativeBasicRuntime(session, observer, emit, persist, collaboration.photos, voice=state['codex_voice'])
            await observer.ensure_ready()
            await runtime.ensure_ready()
        runtime.audit = audit
        if audit:
            audit.attach_diagnostics()
        return runtime
    except BaseException:
        if runtime:
            await runtime.close()
        else:
            if observer:
                await observer.close()
            await collaboration.close()
            await collaboration.photos.close()
        raise


runtime_factory = None if os.environ.get('TUTOR_TEST_MODE') == '1' else create_runtime
bridge = None
finish_jobs = {}
session_start_lock = asyncio.Lock()
audits = {}

def finish_audit(sid):
    audit=audits.pop(sid,None)
    if audit:audit.finish()

def resume_audit(sid):
    if sid in audits:return audits[sid]
    audit=SessionAudit.resume(ROOT/'runtime'/'student-test'/'audit',sid)
    if audit:
        audits[sid]=audit
        audit.attach_diagnostics()
    return audit

def schedule_finalization(student_id,sid,closing_bridge=None):
    if sid in finish_jobs and not finish_jobs[sid].done():return
    task=asyncio.create_task(run_finalization(student_id,sid,closing_bridge))
    finish_jobs[sid]=task
    task.add_done_callback(lambda done:finish_jobs.pop(sid,None) if finish_jobs.get(sid) is done else None)

async def run_finalization(student_id,sid,closing_bridge=None):
    from .post_lesson import memory_payload,records
    audit=audits.get(sid)
    if audit is None and student_id=='student-test':
        try:audit=await asyncio.to_thread(resume_audit,sid)
        except Exception as error:
            diagnostics.warning('audit_resume_failed type=%s',type(error).__name__)
    try:
        job=store.finalization(student_id,sid)
        if not job:return
        if audit:audit.record('finalization_started',stage=job['stage'])
        if not job['snapshot']:
            store.set_finalization_stage(student_id,sid,'stopping')
            if closing_bridge:
                try:
                    cost=await asyncio.wait_for(closing_bridge.close(),25)
                    store.event(student_id,sid,'session_cost',cost)
                    if audit:audit.record('lesson_event',event_kind='session_cost',payload=cost)
                except Exception as error:
                    diagnostics.info('finalization_close_failed type=%s',type(error).__name__)
            elif not job['online']:
                cost={'usd':0,'status':'no_api_calls'}
                store.event(student_id,sid,'session_cost',cost)
                if audit:audit.record('lesson_event',event_kind='session_cost',payload=cost)
            snapshot=store.freeze_finalization(student_id,sid)
            if audit:audit.record('finalization_snapshot_saved',event_count=len(snapshot['events']))
        else:snapshot=json.loads(job['snapshot'])
        if not job['online'] or not records(snapshot):
            store.complete_finalization(student_id,sid,None)
            if audit:audit.record('finalization_completed',summary=None)
            return
        for attempt in range(2):
            try:
                store.set_finalization_stage(student_id, sid, 'saving')
                summary = memory_payload(snapshot)
                store.complete_finalization(student_id, sid, summary)
                if audit:
                    audit.record('finalization_completed', summary=summary)
                return
            except ValueError as error:
                if str(error) != '學習證據已更新' or attempt:
                    raise
                snapshot = store.freeze_finalization(student_id, sid, force=True)
    except asyncio.CancelledError:raise
    except Exception as error:
        diagnostics.info('finalization_failed type=%s',type(error).__name__)
        if audit:audit.record('finalization_failed',error_type=type(error).__name__,message=str(error)[:1000])
        try:store.fail_finalization(student_id,sid,'課後整理失敗，請重試')
        except Exception:diagnostics.exception('finalization_state_save_failed')
    finally:
        finish_audit(sid)

@asynccontextmanager
async def lifespan(app):
    global store, active, bridge
    from .data_lock import DataLock
    data_lock = DataLock(ROOT)
    try:
        if os.environ.get('TUTOR_TEST_MODE') == '1':
            from .test_support import seed_curriculum
            seed_curriculum()
        store = Store(ROOT)
    except BaseException:
        data_lock.close()
        raise
    transport_log=logging.getLogger('tutor.transport')
    audit_log=logging.getLogger('tutor.audit')
    prune_connection_logs(include_current=True)
    diagnostic_handler=ThreeDayRotatingHandler(ROOT/'connection.log')
    diagnostic_formatter=logging.Formatter('%(asctime)sZ %(levelname)s %(message)s',datefmt='%Y-%m-%dT%H:%M:%S')
    diagnostic_formatter.converter=time.gmtime
    diagnostic_handler.setFormatter(diagnostic_formatter)
    transport_log.addHandler(diagnostic_handler)
    transport_log.setLevel(logging.INFO)
    audit_log.addHandler(diagnostic_handler)
    audit_log.setLevel(logging.WARNING)
    if READY_PORT is not None:
        print(json.dumps({'port': READY_PORT}), flush=True)
    audit_root=ROOT/'runtime'/'student-test'/'audit'
    try:
        prune_audits(audit_root,active_ids=audits)
        prune_provider_logs(ROOT/'runtime')
    except OSError as error:
        diagnostics.warning('audit_cleanup_failed type=%s',type(error).__name__)
    def rotate_and_prune_logs():
        diagnostic_handler.acquire()
        try:
            if time.time()-diagnostic_handler.opened_at>=24*60*60:
                diagnostic_handler.doRollover()
            prune_connection_logs()
        finally:diagnostic_handler.release()
    async def audit_cleanup_loop():
        cycles=0
        while True:
            await asyncio.sleep(60)
            cycles+=1
            try:
                await asyncio.to_thread(prune_audits,audit_root,set(audits))
                if cycles%60==0:await asyncio.to_thread(prune_provider_logs,ROOT/'runtime')
                await asyncio.to_thread(rotate_and_prune_logs)
            except OSError as error:
                diagnostics.warning('audit_cleanup_failed type=%s',type(error).__name__)
    audit_cleaner=asyncio.create_task(audit_cleanup_loop())
    watcher = None
    if os.environ.get('TUTOR_PARENT_WATCH') == '1':
        async def monitor():
            await asyncio.to_thread(sys.stdin.buffer.read)
            if _uvicorn_server is not None:
                _uvicorn_server.should_exit = True
                await asyncio.sleep(34)
            os._exit(0)
        watcher = asyncio.create_task(monitor())
    try:
        for job in store.pending_finalizations():
            if job['state']!='failed':schedule_finalization(job['student_id'],job['session_id'])
        yield
    finally:
        for task in list(finish_jobs.values()):task.cancel()
        if finish_jobs:await asyncio.gather(*list(finish_jobs.values()),return_exceptions=True)
        if active:
            try:await finalize_session(active['id'],'interrupted')
            except Exception as error:report_failure('shutdown_runtime',error)
        if watcher: watcher.cancel()
        audit_cleaner.cancel()
        await asyncio.gather(audit_cleaner,return_exceptions=True)
        for sid in list(audits):finish_audit(sid)
        transport_log.removeHandler(diagnostic_handler)
        audit_log.removeHandler(diagnostic_handler)
        diagnostic_handler.close()
        data_lock.close()

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

@app.middleware('http')
async def authentication(request: Request, call_next):
    from starlette.responses import JSONResponse
    if request.method != 'OPTIONS' and (not TOKEN or request.headers.get('authorization') != f'Bearer {TOKEN}'):
        return JSONResponse({'detail':'未授權'},status_code=401)
    photo_upload = request.url.path.startswith('/sessions/') and request.url.path.endswith('/frame')
    limit = 16_100_000 if photo_upload else 8_000_000
    raw_length = request.headers.get('content-length')
    if raw_length is not None:
        try:
            length = int(raw_length)
            if length < 0:raise ValueError
        except ValueError:
            return JSONResponse({'detail':'無效請求長度'},status_code=400)
        if length > limit:
            return JSONResponse({'detail':'請求過大'},status_code=413)
    elif 'chunked' in request.headers.get('transfer-encoding', '').lower():
        if len(await request.body()) > limit:
            return JSONResponse({'detail':'請求過大'},status_code=413)
    return await call_next(request)

app.add_middleware(CORSMiddleware, allow_origins=['null','http://127.0.0.1:5173'], allow_methods=['GET','POST','PUT','PATCH','DELETE'], allow_headers=['Authorization','Content-Type'])

def current(session_id):
    if not active or active['id'] != session_id:
        raise HTTPException(409,'此陪讀已結束或切換')
    return dict(active)

@app.get('/curriculum/coverage')
def curriculum_coverage():
    return {**coverage(), 'concept_catalog': catalog_status()}

@app.get('/curriculum/concepts')
def curriculum_concepts(grade: int, subject: str, q: str=''):
    if not 1 <= grade <= 12 or len(subject)>40 or len(q)>80:
        raise HTTPException(422,'年級、科目或查詢長度無效')
    return {'results':concepts(grade,subject,q.strip()),
            'review':'automatic_unreviewed'}

@app.get('/curriculum/search')
def curriculum_search(q: str, subject: str=''):
    if not 2 <= len(q.strip()) <= 80 or len(subject)>40:raise HTTPException(422,'請輸入 2–80 字的查詢')
    return {'results':search(q.strip(),subject)}

@app.get('/bootstrap')
def bootstrap():
    students=store.students()
    schools={school['id']: {
        'year': None, 'source': '',
        'notice': '這所學校尚未匯入教材與行事曆資料，請依孩子的課本與學校通知確認。',
        'textbooks': {}, 'events': [],
        **json.loads(school['context_json']), 'school': school['name']}
        for school in store.schools()}
    seed_file = Path(os.environ['TUTOR_BOOTSTRAP_FILE']) if os.environ.get('TUTOR_BOOTSTRAP_FILE') else ROOT / 'bootstrap.json'
    seed = json.loads(seed_file.read_text(encoding='utf-8')) if seed_file.exists() else {}
    teacher_defaults={s['id']:student_profile(s['id'], seed=seed) for s in students}
    from .agy_profile import agy_executable, agy_onboarding
    from .providers import ProviderError
    agy_ready, agy_issue = True, ''
    try:
        agy_executable()
        agy_onboarding()
    except ProviderError as error:
        agy_ready, agy_issue = False, str(error)
    return {'students': students, 'schools': schools, 'teacher_defaults': teacher_defaults,
            'codex_voice_options': [{'id': voice, 'label': voice.capitalize()} for voice in NATIVE_VOICES],
            'capabilities': {'codex': {'ready': True}, 'agy': {'ready': agy_ready, 'reason': agy_issue}}}


def do_clear_student_history(student_id: str):
    if active and active['student_id']==student_id: raise HTTPException(409,'請先結束陪讀')
    if any(job['student_id']==student_id and job['state']!='failed' for job in store.pending_finalizations()):
        raise HTTPException(409,'請先完成課後整理')
    if student_id not in {student['id'] for student in store.students()}:
        raise ValueError('學生不存在')
    raw_runtime=ROOT/'runtime'
    if any(part.is_symlink() or (hasattr(part,'is_junction') and part.is_junction())
           for part in (raw_runtime,*raw_runtime.parents)):
        raise HTTPException(409,'陪讀暫存目錄位置異常，未刪除紀錄')
    runtime_root=raw_runtime.resolve()
    workspace=runtime_root/student_id
    if workspace.exists() or workspace.is_symlink():
        if (workspace.is_symlink() or (hasattr(workspace,'is_junction') and workspace.is_junction())
                or not workspace.resolve().is_relative_to(runtime_root)):
            raise HTTPException(409,'陪讀暫存目錄位置異常，未刪除紀錄')
        try:
            shutil.rmtree(workspace)
        except OSError as error:
            raise HTTPException(409,'陪讀暫存目錄無法清除，學生紀錄仍保留') from error
    with store.student(student_id) as db:
        db.execute('DELETE FROM learning_sources')
        db.execute('DELETE FROM learning_edits')
        db.execute('DELETE FROM learning_evidence')
        db.execute('DELETE FROM learning_overrides')
        db.execute('DELETE FROM learning_attempt_edits')
        db.execute('DELETE FROM learning_attempts')
        db.execute('DELETE FROM session_finalizations')
        db.execute('DELETE FROM session_reviews')
        db.execute('DELETE FROM events');db.execute('DELETE FROM sessions')

class Start(BaseModel):
    student_id: str
    mode: Literal['basic'] = 'basic'
    basic_voice: Literal['codex', 'agy'] = 'codex'
    notes: str = Field(default='', max_length=4000)
    teacher_name: str = Field(default='', max_length=40)
    codex_voice: str = Field(default='', max_length=40)
    resume_session_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')


@app.get('/students/{student_id}/resumable-session')
def resumable_session(student_id: str):
    from .collaboration_cli import safe_path
    try:saved=store.resumable(student_id)
    except ValueError as error:raise HTTPException(404,str(error)) from error
    if not saved:return None
    checkpoint=safe_path(ROOT/'runtime'/student_id, 'collaboration/lessons/' + saved['id'] + '/internal/checkpoint.json')
    if not checkpoint.is_file():return None
    return {'id':saved['id'],'mode':saved['mode'],'started':saved['started']}

@app.post('/sessions')
async def start(body: Start):
    # Model discovery and provider startup yield to other requests. Keep the
    # single-session check and publication in the same critical section.
    async with session_start_lock:
        return await start_session(body)

async def start_session(body: Start):
    global active, bridge
    if active: raise HTTPException(409,'請先結束目前的陪讀')
    if any(job['state']!='failed' for job in store.pending_finalizations()):
        raise HTTPException(409,'請先完成課後整理')
    if body.resume_session_id:
        from .collaboration_cli import safe_path
        try:saved=store.resumable(body.student_id, body.resume_session_id)
        except ValueError as error:raise HTTPException(404,str(error)) from error
        if not saved:raise HTTPException(409,'此陪讀無法接續，請開始新的陪讀')
        checkpoint=safe_path(ROOT/'runtime'/body.student_id, 'collaboration/lessons/' + saved['id'] + '/internal/checkpoint.json')
        if not checkpoint.is_file():raise HTTPException(409,'找不到接續筆記，請開始新的陪讀')
        config = saved['config']
        engine = config.get('basic_voice')
        body = body.model_copy(update={
            'notes': saved['notes'], 'teacher_name': config.get('teacher_name', ''),
            'basic_voice': engine if engine in ('codex', 'agy') else body.basic_voice,
            'codex_voice': config.get('codex_voice', '') if engine == 'codex' else body.codex_voice,
        })
    if body.basic_voice == 'codex' and body.codex_voice and body.codex_voice not in NATIVE_VOICES:
        raise HTTPException(422, '不支援的 Codex 原生聲線')
    teacher_name = body.teacher_name.strip() or student_profile(body.student_id, ROOT)['teacher_name']
    try:
        sid = (store.resume(body.student_id, body.resume_session_id) if body.resume_session_id
               else store.start(body.student_id, body.mode, body.notes))
    except ValueError as error:
        raise HTTPException(409 if body.resume_session_id else 404, str(error)) from error
    effective_backend = body.basic_voice
    state = {'id': sid, 'student_id': body.student_id, 'mode': 'basic', 'backend': effective_backend,
              'teacher_name': teacher_name, 'resumed': bool(body.resume_session_id),
              'basic_voice': body.basic_voice, 'codex_voice': body.codex_voice if body.basic_voice == 'codex' else ''}
    if body.student_id=='student-test':
        audit_root=ROOT/'runtime'/'student-test'/'audit'
        try:prune_audits(audit_root,active_ids=audits)
        except OSError as error:diagnostics.warning('audit_cleanup_failed type=%s',type(error).__name__)
        try:
            workspace=ROOT/'runtime'/'student-test'/'collaboration'/'lessons'/sid
            audit = resume_audit(sid) if body.resume_session_id else None
            if audit is None:
                audit=SessionAudit(audit_root,sid,body.mode,effective_backend,workspace)
                audits[sid]=audit
            if audit.record('session_config',notes=body.notes,teacher_name=teacher_name,
                            basic_voice=body.basic_voice,codex_voice=state['codex_voice'],
                            grade=next(s['grade'] for s in store.students() if s['id']==body.student_id)) is None:
                raise OSError('Cannot write test-session configuration')
        except Exception as error:
            if sid in audits:finish_audit(sid)
            store.finish(body.student_id,sid,'interrupted' if body.resume_session_id else 'ended')
            raise HTTPException(500,'測試紀錄無法建立，陪讀未開始') from error
    new_bridge = None
    try:
        store.event(body.student_id, sid, 'session_config',
                    {key: state[key] for key in ('backend', 'mode', 'teacher_name', 'basic_voice', 'codex_voice')})
        if runtime_factory is not None:
            runtime=await runtime_factory({**state,'notes':body.notes})
            async def expire_session():
                # A renderer may disappear before it can call the REST end route.
                # Release the global session as well as the provider after the retry window.
                if active and active['id']==sid:
                    await finalize_session(sid,'interrupted')
            if runtime:
                new_bridge=RuntimeBridge(runtime,on_expire=expire_session)
                new_bridge.arm_expiry(timeout=90)
    except BaseException as error:
        diagnostics.info('runtime_create_failed type=%s',type(error).__name__)
        if sid in audits:
            audits[sid].record('runtime_create_failed',error_type=type(error).__name__,message=str(error)[:1000])
            finish_audit(sid)
        store.finish(body.student_id,sid,'interrupted' if body.resume_session_id else 'ended')
        if not isinstance(error, Exception):
            raise
        from .providers import ProviderError
        if isinstance(error,ProviderError):
            raise HTTPException(503,str(error)) from error
        raise HTTPException(503,'教學服務啟動失敗，裝置尚未開始傳送') from error
    active,bridge=state,new_bridge
    from .lesson import lesson_state
    return {**active,'online':bridge is not None,
            'lesson':lesson_state(bridge.runtime.session) if bridge else None}

@app.websocket('/sessions/{sid}/stream')
async def stream(sid: str, ws: WebSocket):
    if not active or active['id']!=sid or bridge is None:
        await ws.close(code=4409);return
    await bridge.serve(ws,TOKEN)

class PhotoFrameRequest(BaseModel):
    image: str = Field(max_length=16_000_000)
    request_id: str | None = Field(default=None,min_length=1,max_length=64)
    capture_meta: dict | None = None

@app.post('/sessions/{sid}/frame')
async def upload_photo(sid: str, body: PhotoFrameRequest):
    current(sid)
    if bridge is None or bridge.closed or not bridge.connected or bridge.detaching:
        raise HTTPException(409,'圖片連線尚未就緒')
    runtime=bridge.runtime
    if not runtime.session.active or not runtime.camera_enabled:
        raise HTTPException(409,'相機已關閉')
    if body.capture_meta is not None and len(json.dumps(body.capture_meta, ensure_ascii=False))>1000:
        raise HTTPException(422,'無效拍攝資訊')
    try:
        if body.request_id is None and isinstance(runtime, LiveClassroomRuntime):
            accepted=runtime.accept_background_frame(body.image, body.capture_meta)
        else:
            accepted=runtime.accept_frame(body.image, body.request_id, body.capture_meta)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    if accepted:
        diagnostics.info('photo_http_received image_chars=%d requested=%s',len(body.image),bool(body.request_id))
    return {'accepted':bool(accepted)}

@app.post('/sessions/{sid}/end')
async def end(sid: str):
    return await finalize_session(sid,'ended')

class FinishRequest(BaseModel):
    captured: int = Field(default=0,ge=0,le=100000)
    submitted_to_runtime: int = Field(default=0,ge=0,le=100000)
    last_playback: dict | None = None

def public_finalization(job):
    return {'session_id':job['session_id'],'state':job['state'],'stage':job['stage'],
            'updated_at':job['updated'],'snapshot_saved':bool(job['snapshot']),
            'summary_available':bool(job['summary']),'online':bool(job['online']),
            'error':job['error'],'progress':json.loads(job['progress']) if job.get('progress') else None}

@app.post('/sessions/{sid}/finalize')
async def begin_finalization(sid:str,body:FinishRequest):
    global active,bridge
    async with session_start_lock:
        student_id,existing=store.locate_finalization(sid)
        if existing:return public_finalization(existing)
        state=current(sid)
        student_id=state['student_id']
        online=bridge is not None
        try:
            playback=body.last_playback
            if playback is not None and (not isinstance(playback.get('playback_id'),str) or
                    len(playback['playback_id'])>80 or not isinstance(playback.get('heard'),str) or
                    len(playback['heard'])>600):raise HTTPException(422,'播放回報格式錯誤')
            job=store.begin_finalization(student_id,sid,online,state['backend'],
                {'captured':body.captured,'submitted_to_runtime':body.submitted_to_runtime},playback)
            if sid in audits:
                audits[sid].record('finalization_requested',captured=body.captured,
                                   submitted_to_runtime=body.submitted_to_runtime,last_playback=playback)
        except ValueError as error:raise HTTPException(409,str(error))
        closing_bridge=bridge
        active=None;bridge=None
    schedule_finalization(student_id,sid,closing_bridge)
    return public_finalization(job)

@app.get('/sessions/{sid}/finalization')
def finalization_status(sid:str):
    _,job=store.locate_finalization(sid)
    if not job:raise HTTPException(404,'找不到課後整理工作')
    return public_finalization(job)

@app.get('/finalizations/pending')
def pending_finalizations():
    return {'items':[public_finalization(job) for job in store.pending_finalizations()]}

@app.post('/sessions/{sid}/finalization/retry')
async def retry_finalization(sid:str):
    student_id,job=store.locate_finalization(sid)
    if not job:raise HTTPException(404,'找不到課後整理工作')
    try:store.retry_finalization(student_id,sid)
    except ValueError as error:raise HTTPException(409,str(error))
    schedule_finalization(student_id,sid)
    return public_finalization(store.finalization(student_id,sid))

@app.post('/sessions/{sid}/finalization/dismiss')
def dismiss_finalization(sid:str):
    student_id,job=store.locate_finalization(sid)
    if not job or job['state']!='failed':raise HTTPException(409,'此課後整理不能略過')
    store.dismiss_finalization(student_id,sid)
    return {'dismissed':True}

async def finalize_session(sid: str,status: str):
    global active, bridge
    async with session_start_lock:
        if not active: return {'ended':True}
        state=current(sid)
        current_bridge=bridge
        bridge=None
        # Invalidate global routing before awaiting cancellation of provider jobs.
        active=None
        cost={'usd':0,'status':'no_api_calls'}
        try:
            if current_bridge:
                cost=await asyncio.wait_for(current_bridge.close(),25)
            store.event(state['student_id'],sid,'session_cost',cost)
            if sid in audits:audits[sid].record('lesson_event',event_kind='session_cost',payload=cost)
        finally:
            store.finish(state['student_id'],sid,status)
            if sid in audits:audits[sid].record('session_ended',status=status)
            finish_audit(sid)
    diagnostics.info('session_ended mode=%s backend=%s',state['mode'],state['backend'])
    return {'ended':True,'cost':cost}

class Event(BaseModel):
    kind: str
    payload: dict

@app.post('/sessions/{sid}/events')
async def event(sid: str, body: Event):
    state=current(sid)
    if body.kind not in ('playback_interrupted','device_error','focus_exit','student_note','capture_stats','client_diagnostic'):
        raise HTTPException(422,'不支援的事件')
    if len(json.dumps(body.payload, ensure_ascii=False))>10000: raise HTTPException(413,'事件過大')
    store.event(state['student_id'],sid,body.kind,body.payload)
    if sid in audits:audits[sid].record('lesson_event',event_kind=body.kind,payload=body.payload)
    return {'saved':True}

@app.get('/students/{student_id}/history')
def history(student_id: str):
    try: return {**store.history(student_id),'learning_context':store.learning_context(student_id),
                 'learning_profile':store.learning_profile(student_id),
                 'learning_evidence':store.learning_evidence(student_id),
                 'practice_attempts':store.learning_attempts(student_id),
                 'practice_settings':store.practice_settings(student_id),
                 'practice_edits':store.practice_edits(student_id)}
    except ValueError as e: raise HTTPException(404,str(e))

class SessionContextReview(BaseModel):
    include: bool
    reason: str = Field(default='',max_length=300)

@app.put('/students/{student_id}/sessions/{session_id}/context')
async def review_session_context(student_id: str,session_id: str,body: SessionContextReview):
    try:
        store.set_session_context(student_id,session_id,body.include,body.reason)
        await refresh_learning_memory(student_id)
        return {'saved':True,'include':body.include}
    except ValueError as e:raise HTTPException(404,str(e))

async def refresh_learning_memory(student_id):
    if active and active['student_id']==student_id and bridge:
        bridge.runtime.session.long_term_memory=store.learning_profile(student_id)
        bridge.runtime.session.memory=store.learning_context(student_id)
        settings=store.practice_settings(student_id)
        bridge.runtime.session.practice_checks_enabled=settings['enabled']
        bridge.runtime.session.reviewed_teaching_cards=settings['reviewed_cards']
        bridge.runtime.session.learning_attempts=store.learning_attempts(student_id,context_only=True)
        bridge.runtime.session.plan_revision+=1
        collaboration=getattr(bridge.runtime.session,'collaboration',None)
        if collaboration:collaboration.invalidate_memory()
        sync=getattr(bridge.runtime,'sync_context',None)
        if sync:await sync()

class LearningOverride(BaseModel):
    subject: str = Field(min_length=1,max_length=40)
    label: str = Field(min_length=1,max_length=120)
    status: Literal['unassessed','practicing','needs_review','parent_confirmed']
    note: str = Field(default='',max_length=500)

@app.put('/students/{student_id}/learning/{code}')
async def update_learning(student_id: str,code: str,body: LearningOverride):
    try:
        store.set_learning_override(student_id,code,body.subject,body.label,body.status,body.note)
        await refresh_learning_memory(student_id)
    except ValueError as e:raise HTTPException(422,str(e))
    return {'saved':True}

class EvidenceCorrection(BaseModel):
    quote: str = Field(min_length=1,max_length=500)

@app.patch('/students/{student_id}/learning/evidence/{evidence_id}')
async def correct_evidence(student_id: str,evidence_id: str,body: EvidenceCorrection):
    try:
        store.correct_learning_evidence(student_id,evidence_id,body.quote)
        await refresh_learning_memory(student_id)
    except ValueError as e:raise HTTPException(404,str(e))
    return {'saved':True}

@app.delete('/students/{student_id}/learning/evidence/{evidence_id}')
async def exclude_evidence(student_id: str,evidence_id: str):
    try:
        store.correct_learning_evidence(student_id,evidence_id,exclude=True)
        await refresh_learning_memory(student_id)
    except ValueError as e:raise HTTPException(404,str(e))
    return {'excluded':True}


class PracticeSettings(BaseModel):
    enabled: bool
    reviewed_cards: list[str] = Field(default_factory=list,max_length=20)


class PracticeAttempt(BaseModel):
    model_config = {'extra':'forbid'}
    subject: str = Field(min_length=1,max_length=40)
    concept: str = Field(min_length=1,max_length=120)
    question: str = Field(min_length=1,max_length=1000)
    response: str = Field(default='',max_length=1000)
    assistance: Literal['none','read_prompt','hint','worked_example','full_solution']
    phase: Literal['initial','practice','immediate_check','delayed_check']
    previous_attempt_id: str = Field(default='',max_length=40)
    answer_basis: str = Field(default='',max_length=1000)


class NewPracticeAttempt(PracticeAttempt):
    session_id: str


class PracticeReview(PracticeAttempt):
    result: Literal['unreviewed','correct','incorrect','not_observed']
    note: str = Field(default='',max_length=1000)
    revision: int = Field(ge=1)
    exclude: bool = False


@app.get('/students/{student_id}/practice')
def practice(student_id: str):
    from .teaching_cards import CARDS
    try:
        settings=store.practice_settings(student_id)
        profile=next(s for s in store.students() if s['id']==student_id)
        return {'settings':settings,'attempts':store.learning_attempts(student_id),
                'cards':[card for card in CARDS if profile['grade'] in card['grades']],
                'voice_evidence_note':'agy 語音沒有可核對逐字稿；可用學生文字求助，或由家長補記。'}
    except ValueError as e:raise HTTPException(404,str(e))


@app.put('/students/{student_id}/practice/settings')
async def practice_settings(student_id: str,body: PracticeSettings):
    try:
        settings=store.save_practice_settings(student_id,body.enabled,body.reviewed_cards)
        await refresh_learning_memory(student_id)
        return settings
    except ValueError as e:raise HTTPException(422,str(e))


@app.post('/students/{student_id}/practice/attempts')
async def record_practice(student_id: str,body: NewPracticeAttempt):
    from .learning_practice import ATTEMPT_FIELDS
    try:
        ident=store.record_learning_attempt(student_id,body.session_id,
            {key:getattr(body,key) for key in ATTEMPT_FIELDS})
        await refresh_learning_memory(student_id)
        return {'id':ident,'saved':ident is not None}
    except ValueError as e:raise HTTPException(422,str(e))


@app.put('/students/{student_id}/practice/attempts/{attempt_id}')
async def review_practice(student_id: str,attempt_id: str,body: PracticeReview):
    from .learning_practice import ATTEMPT_FIELDS
    try:
        store.review_learning_attempt(student_id,attempt_id,
            {key:getattr(body,key) for key in ATTEMPT_FIELDS},body.result,body.note,body.revision,body.exclude)
        await refresh_learning_memory(student_id)
        return {'saved':True}
    except ValueError as e:raise HTTPException(422,str(e))

class Profile(BaseModel):
    name: str = Field(min_length=1,max_length=40)
    preferences: str = Field(default='',max_length=4000)
    grade: int | None = Field(default=None, ge=1, le=12)
    school_name: str | None = Field(default=None, max_length=100)

class NewStudent(Profile):
    grade: int = Field(ge=1, le=12)

@app.post('/students')
def add_student(body: NewStudent):
    try:
        return store.add_student(body.name, body.grade, body.preferences, (body.school_name or '').strip())
    except ValueError as error:
        raise HTTPException(422, str(error))

@app.post('/demo')
def load_demo():
    global store
    if active or store.students():
        raise HTTPException(409, '展示資料僅能載入空白資料庫，避免混入已有學生。')
    source = Path(__file__).resolve().parents[1] / 'examples' / 'demo.json'
    content = source.read_text(encoding='utf-8')
    try:
        with (ROOT / 'bootstrap.json').open('x', encoding='utf-8') as file:
            file.write(content)
    except FileExistsError:
        raise HTTPException(409, '已有私有初始化設定，請使用獨立資料位置載入展示。')
    store = Store(ROOT)
    return {'loaded': True, 'synthetic': True}

@app.put('/students/{student_id}')
def profile(student_id: str, body: Profile):
    student=next((s for s in store.students() if s['id']==student_id),None)
    if student is None: raise HTTPException(404,'學生不存在')
    if not body.name.strip():raise HTTPException(422,'稱呼不可空白')
    school_name=student['school_name'] if body.school_name is None else body.school_name.strip()
    store.save_profile(student_id,body.name.strip(),body.preferences,body.grade,school_name)
    return {'saved':True}

@app.delete('/students/{student_id}/history')
async def clear_history(student_id: str):
    async with session_start_lock:
        try: do_clear_student_history(student_id)
        except ValueError as e: raise HTTPException(404,str(e))
    return {'deleted':True}

if __name__=='__main__':
    if not TOKEN: raise SystemExit('TUTOR_CORE_TOKEN is required')
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
    READY_PORT = sock.getsockname()[1]
    _uvicorn_server = uvicorn.Server(uvicorn.Config(app,log_level='warning',access_log=False,timeout_graceful_shutdown=8))
    _uvicorn_server.run(sockets=[sock])
