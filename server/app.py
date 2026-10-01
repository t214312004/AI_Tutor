import asyncio
import base64
import io
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

import edge_tts
import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from .storage import Store
from .curriculum import coverage, search
from .curriculum_map import concepts, catalog_status
from .transport import RuntimeBridge
from .identity import EDGE_VOICE, student_profile, VOICE_OPTIONS, ALLOWED_VOICES, resolve_voice
from .basic import sanitize_transcript
from .audit import SessionAudit,AuditedProvider,prune_audits,prune_provider_logs
from .paths import data_dir

ROOT = data_dir()
TOKEN = os.environ.get('TUTOR_CORE_TOKEN', '')
store = None
active = None
turn_generation = 0
keys = {}
READY_PORT = None
diagnostics=logging.getLogger('tutor.transport')
LOG_RETENTION_SECONDS=72*60*60

class ThreeDayRotatingHandler(RotatingFileHandler):
    def __init__(self,filename):
        if Path(filename).is_symlink():raise OSError('Connection log path is a link')
        super().__init__(filename,maxBytes=1_000_000,backupCount=2,encoding='utf-8')
        self.opened_at=Path(filename).stat().st_ctime
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
            for line in lines:
                try:created=datetime.fromisoformat(line[:19]).replace(tzinfo=timezone.utc).timestamp()
                except ValueError:created=fallback
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
    from .basic import BasicRuntime,SpeechService
    from .providers import LazyCodex,AgyTutor,FailoverTutor
    from .tutor import TutorSession
    from .live_runtime import LiveRuntime
    from .basic_workspace import BasicWorkspace,prepare_student_workspace
    required={'basic':'GROQ_API_KEY','gpt':'OPENAI_API_KEY','gemini':'GEMINI_API_KEY'}[state['mode']]
    diagnostics.info('runtime_create_begin mode=%s backend=%s',state['mode'],state['backend'])
    if not api_key(required):return None
    audit=audits.get(state['id'])
    profile=next(s for s in store.students() if s['id']==state['student_id'])
    session=TutorSession(state['student_id'],profile['grade'],profile['preferences']+'\n'+state['notes'],id=state['id'])
    session.student_name=profile['name']
    session.teacher_name=state.get('teacher_name') or ''
    session.edge_voice=state.get('edge_voice') or ''
    session.basic_workflow=state['mode']=='basic'
    session.memory=store.learning_context(state['student_id'])
    session.long_term_memory=store.learning_profile(state['student_id'])
    provider=FailoverTutor if state['mode']!='basic' else (AgyTutor if state['backend']=='agy' else LazyCodex)
    workspace=(await asyncio.to_thread(prepare_student_workspace,ROOT/'runtime'/state['student_id'])).parent if state['mode']=='basic' else ROOT/'runtime'/state['student_id']/state['id']
    photo_workspace=BasicWorkspace(workspace/'teacher',teacher_name=session.teacher_name) if state['mode']=='basic' else None
    if photo_workspace:session.workspace_context=photo_workspace.continuity()
    if audit:audit.capture_guide()
    gpt_config=state.get('gpt_live')
    if state['mode']=='gpt' and gpt_config:
        from .openai_vision import OpenAIResponseVision
        def selected(route, path):
            kind=route.get('provider', 'openai')
            if kind=='openai':return OpenAIResponseVision(api_key('OPENAI_API_KEY'),route['model'],route['effort'])
            if kind=='codex':return LazyCodex(path,route['model'],route['effort'])
            return AgyTutor(path,route['model'],route['effort'])
        observer=selected(gpt_config['vision'],workspace/'observer')
        teacher=(selected(gpt_config['delegation'],workspace/'teacher')
                 if gpt_config['delegation']['type']=='client' else observer)
    else:
        teacher=provider(workspace/'teacher')
        # One Basic class is one native CLI conversation, including camera observations.
        observer=teacher if state['mode']=='basic' else provider(workspace/'observer')
    if audit:
        shared_provider=teacher is observer
        teacher=AuditedProvider(teacher,audit,'observer' if shared_provider and state['mode']=='gpt' else 'teacher')
        observer=teacher if shared_provider else AuditedProvider(observer,audit,'observer')
    if state['mode']=='basic' and hasattr(teacher,'ensure_ready'):
        try:await teacher.ensure_ready()
        except BaseException:
            await photo_workspace.close()
            raise
    if state['mode']=='gpt' and gpt_config:
        for selected_provider in (teacher, observer) if teacher is not observer else (teacher,):
            if hasattr(selected_provider,'ensure_ready'):
                try:await selected_provider.ensure_ready()
                except BaseException:
                    await teacher.close()
                    if observer is not teacher:await observer.close()
                    raise
    diagnostics.info('runtime_create_ready mode=%s backend=%s',state['mode'],state['backend'])
    async def emit(event):pass
    def persist(kind,payload):
        if kind=='learning_evidence':
            store.record_learning_evidence(state['student_id'],state['id'],payload)
        else:store.event(state['student_id'],state['id'],kind,payload)
        if audit:audit.record('lesson_event',event_kind=kind,payload=payload)
    if state['mode']=='basic':
        runtime=BasicRuntime(session,teacher,observer,SpeechService(api_key(required),teacher_name=session.teacher_name,voice=session.edge_voice),emit,persist,photo_workspace)
    else:
        runtime=LiveRuntime(session,teacher,observer,state['mode'],api_key(required),emit,persist,
                            gpt_config=gpt_config)
        if audit:audit.record('live_instructions',instructions=runtime.provider.instructions)
    runtime.audit=audit
    if audit:audit.attach_diagnostics()
    return runtime

runtime_factory = create_runtime
bridge = None
diagnostic_bridge = None
finish_jobs = {}
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
    from .post_lesson import analyze,memory_payload,records
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
        workspace=ROOT/'runtime'/student_id/'post-lesson'/sid
        failure=None
        for attempt in range(2):
            try:
                analysis,provider_name,turns=await asyncio.wait_for(analyze(snapshot,job['backend'],workspace,
                    audit=audit,openai_key=api_key('OPENAI_API_KEY'),
                    usage_callback=lambda item:store.event(student_id,sid,'post_lesson_api_usage',item)),360)
                store.set_finalization_stage(student_id,sid,'saving')
                summary=memory_payload(snapshot,analysis,provider_name,turns)
                store.complete_finalization(student_id,sid,summary)
                if audit:audit.record('finalization_completed',summary=summary)
                return
            except asyncio.CancelledError:raise
            except Exception as error:
                failure=error
                diagnostics.info('finalization_analysis_failed attempt=%s type=%s',attempt+1,type(error).__name__)
                if str(error)=='學習證據已更新':
                    snapshot=store.freeze_finalization(student_id,sid,force=True)
                if attempt==0:await asyncio.sleep(2)
        raise RuntimeError('AI 整理服務未能完成') from failure
    except asyncio.CancelledError:raise
    except Exception as error:
        diagnostics.info('finalization_failed type=%s',type(error).__name__)
        if audit:audit.record('finalization_failed',error_type=type(error).__name__,message=str(error)[:1000])
        try:store.fail_finalization(student_id,sid,'課後整理失敗，請重試')
        except Exception:diagnostics.exception('finalization_state_save_failed')
    finally:
        try:
            post_workspace=ROOT/'runtime'/student_id/'post-lesson'/sid
            if audit and post_workspace.is_dir():
                await asyncio.to_thread(audit.snapshot_workspace,post_workspace,'post-lesson-workspace')
        finally:finish_audit(sid)

@asynccontextmanager
async def lifespan(app):
    global store
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
    async def audit_cleanup_loop():
        cycles=0
        while True:
            await asyncio.sleep(60)
            cycles+=1
            try:
                await asyncio.to_thread(prune_audits,audit_root,set(audits))
                if cycles%60==0:await asyncio.to_thread(prune_provider_logs,ROOT/'runtime')
                diagnostic_handler.acquire()
                try:
                    if time.time()-diagnostic_handler.opened_at>=24*60*60:
                        diagnostic_handler.doRollover()
                    prune_connection_logs()
                finally:diagnostic_handler.release()
            except OSError as error:
                diagnostics.warning('audit_cleanup_failed type=%s',type(error).__name__)
    audit_cleaner=asyncio.create_task(audit_cleanup_loop())
    watcher = None
    if os.environ.get('TUTOR_PARENT_WATCH') == '1':
        async def monitor():
            await asyncio.to_thread(sys.stdin.buffer.read)
            os._exit(0)
        watcher = asyncio.create_task(monitor())
    try:
        for job in store.pending_finalizations():
            if job['state']!='failed':schedule_finalization(job['student_id'],job['session_id'])
        yield
    finally:
        for task in list(finish_jobs.values()):task.cancel()
        if finish_jobs:await asyncio.gather(*list(finish_jobs.values()),return_exceptions=True)
        if watcher: watcher.cancel()
        audit_cleaner.cancel()
        await asyncio.gather(audit_cleaner,return_exceptions=True)
        for sid in list(audits):finish_audit(sid)
        transport_log.removeHandler(diagnostic_handler)
        audit_log.removeHandler(diagnostic_handler)
        diagnostic_handler.close()
        data_lock.close()

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=['null','http://127.0.0.1:5173'], allow_methods=['GET','POST','PUT','DELETE'], allow_headers=['Authorization','Content-Type'])

@app.middleware('http')
async def authentication(request: Request, call_next):
    from starlette.responses import JSONResponse
    if request.method != 'OPTIONS' and (not TOKEN or request.headers.get('authorization') != f'Bearer {TOKEN}'):
        return JSONResponse({'detail':'未授權'},status_code=401)
    length = int(request.headers.get('content-length','0'))
    photo_upload = request.url.path.startswith('/sessions/') and request.url.path.endswith('/frame')
    if length > (16_100_000 if photo_upload else 8_000_000):
        return JSONResponse({'detail':'請求過大'},status_code=413)
    return await call_next(request)

def current(session_id):
    if not active or active['id'] != session_id:
        raise HTTPException(409,'此陪讀已結束或切換')
    return dict(active)

def api_key(name):
    return keys.get(name) or os.environ.get(name, '')

@app.get('/health')
def health():
    return {'status':'ok','version':'0.1.0','pid':os.getpid()}

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
    teacher_defaults={s['id']:student_profile(s['id'], ROOT) for s in students}
    return {'students':students,'schools':schools,'teacher_defaults':teacher_defaults,'voice_options':VOICE_OPTIONS,'capabilities':{
      'basic':{'ready':bool(api_key('GROQ_API_KEY')),'reason':'Groq 金鑰已設定時，開始陪讀會先檢查所選 Codex 或 agy 是否已登入並可回應；教學與作業看圖使用所選後端，播音使用 Edge TTS。','groq_key':bool(api_key('GROQ_API_KEY'))},
      'gpt':{'ready':bool(api_key('OPENAI_API_KEY')),'reason':'GPT-LIVE-1 需要 OpenAI 金鑰；教學與圖片服務依下方選項分別檢查。','key':bool(api_key('OPENAI_API_KEY'))},
      'gemini':{'ready':bool(api_key('GEMINI_API_KEY')),'reason':'已接線；原始作業照片由 Gemini 直接辨識，文字教學後端由 agy 優先、Codex 備援。填入 Gemini 金鑰後可連線。','key':bool(api_key('GEMINI_API_KEY'))}
    }}

@app.get('/gpt-options')
async def gpt_options():
    from .gpt_config import route_options
    return await route_options(ROOT/'model-discovery')

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
        db.execute('DELETE FROM learning_edits')
        db.execute('DELETE FROM learning_evidence')
        db.execute('DELETE FROM learning_overrides')
        db.execute('DELETE FROM session_finalizations')
        db.execute('DELETE FROM events');db.execute('DELETE FROM sessions')

class Start(BaseModel):
    student_id: str
    mode: str
    backend: Literal['codex','agy'] = 'codex'
    notes: str = Field(default='',max_length=4000)
    teacher_name: str = Field(default='',max_length=40)
    edge_voice: str = Field(default='',max_length=60)
    gpt_live: dict | None = None

@app.post('/sessions')
async def start(body: Start):
    global active, turn_generation, bridge
    if active: raise HTTPException(409,'請先結束目前的陪讀')
    if any(job['state']!='failed' for job in store.pending_finalizations()):
        raise HTTPException(409,'請先完成課後整理')
    if body.mode not in ('basic','gpt','gemini'): raise HTTPException(422,'無效模式')
    gpt_config=None
    if body.mode=='gpt' and body.gpt_live is not None:
        from .gpt_config import route_options,validate_route,verify_openai_models
        if not api_key('OPENAI_API_KEY'):
            raise HTTPException(503,'未設定 OPENAI_API_KEY，無法啟動 GPT-LIVE-1')
        submitted_vision=body.gpt_live.get('vision')
        submitted_delegation=body.gpt_live.get('delegation')
        requested={(submitted_vision or {}).get('provider') if isinstance(submitted_vision,dict) else None,
                   (submitted_delegation or {}).get('provider') if isinstance(submitted_delegation,dict) else None}
        providers=tuple(provider for provider in ('codex','agy') if provider in requested)
        try:gpt_config=validate_route(body.gpt_live,await route_options(ROOT/'model-discovery',providers))
        except ValueError as error:raise HTTPException(422,str(error)) from error
        try:await verify_openai_models(api_key('OPENAI_API_KEY'),gpt_config)
        except Exception as error:
            from .providers import ProviderError
            if isinstance(error,ProviderError):raise HTTPException(503,str(error)) from error
            raise
    try: sid=store.start(body.student_id,body.mode,body.notes)
    except ValueError as e: raise HTTPException(404,str(e))
    turn_generation+=1
    effective_backend=(body.backend if body.mode=='basic' else
                       ('gpt_configured' if gpt_config else 'agy_codex_failover') if body.mode=='gpt'
                       else 'gemini_native_vision_agy_codex_tutor')
    defaults=student_profile(body.student_id, ROOT)
    teacher_name=body.teacher_name.strip() or defaults['teacher_name']
    edge_voice=resolve_voice(body.edge_voice.strip() if body.edge_voice.strip() in ALLOWED_VOICES else defaults['edge_voice'])
    active={'id':sid,'student_id':body.student_id,'mode':body.mode,'backend':effective_backend,
            'teacher_name':teacher_name,'edge_voice':edge_voice,'gpt_live':gpt_config}
    if body.student_id=='student-test':
        audit_root=ROOT/'runtime'/'student-test'/'audit'
        try:prune_audits(audit_root,active_ids=audits)
        except OSError as error:diagnostics.warning('audit_cleanup_failed type=%s',type(error).__name__)
        try:
            workspace=(ROOT/'runtime'/'student-test'/'basic-shared'/'teacher' if body.mode=='basic'
                       else ROOT/'runtime'/'student-test'/sid)
            audit=SessionAudit(audit_root,sid,body.mode,effective_backend,workspace)
            audits[sid]=audit
            if audit.record('session_config',notes=body.notes,teacher_name=teacher_name,
                            edge_voice=edge_voice,gpt_live=gpt_config,
                            grade=next(s['grade'] for s in store.students() if s['id']==body.student_id)) is None:
                raise OSError('Cannot write test-session configuration')
        except Exception as error:
            if sid in audits:finish_audit(sid)
            store.finish(body.student_id,sid);active=None
            raise HTTPException(500,'測試紀錄無法建立，陪讀未開始') from error
    store.event(body.student_id,sid,'session_config',{'backend':effective_backend,'mode':body.mode,
        'teacher_name':teacher_name,'edge_voice':edge_voice,'gpt_live':gpt_config})
    bridge=None
    if runtime_factory is not None:
        try:
            runtime=await runtime_factory({**active,'notes':body.notes})
            async def expire_session():
                # A renderer may disappear before it can call the REST end route.
                # Release the global session as well as the provider after the retry window.
                if active and active['id']==sid:
                    await finalize_session(sid,'interrupted')
            bridge=RuntimeBridge(runtime,on_expire=expire_session) if runtime else None
        except Exception as error:
            diagnostics.info('runtime_create_failed type=%s',type(error).__name__)
            if sid in audits:
                audits[sid].record('runtime_create_failed',error_type=type(error).__name__,message=str(error)[:1000])
                finish_audit(sid)
            store.finish(body.student_id,sid);active=None
            from .providers import ProviderError
            if isinstance(error,ProviderError):
                raise HTTPException(503,str(error)) from error
            raise HTTPException(503,'教學服務啟動失敗，裝置尚未開始傳送') from error
    return {**active,'generation':turn_generation,'online':bridge is not None}

@app.websocket('/sessions/{sid}/stream')
async def stream(sid: str, ws: WebSocket):
    if not active or active['id']!=sid or bridge is None:
        await ws.close(code=4409);return
    await bridge.serve(ws,TOKEN)

class PhotoFrameRequest(BaseModel):
    image: str = Field(max_length=16_000_000)
    request_id: str | None = Field(default=None,max_length=64)
    capture_meta: dict | None = None

@app.post('/sessions/{sid}/frame')
async def upload_photo(sid: str, body: PhotoFrameRequest):
    current(sid)
    if (bridge is None or bridge.closed or not bridge.connected or bridge.detaching
            or getattr(bridge.runtime,'mode','basic') != 'gpt'):
        raise HTTPException(409,'圖片連線尚未就緒')
    runtime=bridge.runtime
    if not runtime.session.active or not runtime.camera_enabled:
        raise HTTPException(409,'相機已關閉')
    if body.capture_meta is not None and len(json.dumps(body.capture_meta))>1000:
        raise HTTPException(422,'無效拍攝資訊')
    try:runtime.accept_frame(body.image,body.request_id,body.capture_meta)
    except ValueError as error:raise HTTPException(422,str(error)) from error
    diagnostics.info('photo_http_received image_chars=%d requested=%s',len(body.image),bool(body.request_id))
    return {'accepted':True}

@app.websocket('/diagnostics/basic/stream')
async def diagnostic_stream(ws: WebSocket):
    global diagnostic_bridge
    # Adult/developer synthetic audio probe. Does not access any student profile.
    if diagnostic_bridge is not None or not api_key('GROQ_API_KEY'):
        await ws.close(code=4409);return
    from .basic import BasicRuntime,SpeechService
    from .basic_workspace import BasicWorkspace
    from .providers import LazyCodex,AgyTutor,FailoverTutor
    from .tutor import TutorSession
    backend=ws.query_params.get('backend','codex')
    if backend not in ('codex','agy'):
        await ws.close(code=4400);return
    provider=AgyTutor if backend=='agy' else LazyCodex
    async def emit(event):pass
    diagnostic_workspace=BasicWorkspace(ROOT/'diagnostics'/backend/'teacher')
    diagnostic_provider=provider(diagnostic_workspace.root)
    runtime=BasicRuntime(TutorSession('synthetic',2,'合成語音診斷，沒有真實學生資料。',id='diagnostic'),
      diagnostic_provider,diagnostic_provider,SpeechService(api_key('GROQ_API_KEY')),emit,lambda *args:None,diagnostic_workspace)
    diagnostic_bridge=RuntimeBridge(runtime)
    try:await diagnostic_bridge.serve(ws,TOKEN)
    finally:
        await runtime.close();diagnostic_bridge=None

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
            'error':job['error']}

@app.post('/sessions/{sid}/finalize')
async def begin_finalization(sid:str,body:FinishRequest):
    global active,bridge,turn_generation
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
    active=None;bridge=None;turn_generation+=1
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
    global active, turn_generation, bridge
    if not active: return {'ended':True}
    state=current(sid)
    current_bridge=bridge
    bridge=None
    # Invalidate global routing before awaiting cancellation of provider jobs.
    active=None;turn_generation+=1
    try:
        cost=await current_bridge.close() if current_bridge else {'usd':0,'status':'no_api_calls'}
        store.event(state['student_id'],sid,'session_cost',cost)
        if sid in audits:audits[sid].record('lesson_event',event_kind='session_cost',payload=cost)
    finally:
        store.finish(state['student_id'],sid,status)
        if sid in audits:audits[sid].record('session_ended',status=status)
        finish_audit(sid)
    diagnostics.info('session_ended mode=%s backend=%s',state['mode'],state['backend'])
    return {'ended':True,'cost':cost}

@app.post('/sessions/{sid}/interrupt')
async def interrupt(sid: str):
    global turn_generation
    current(sid);turn_generation+=1
    return {'generation':turn_generation}

class Event(BaseModel):
    kind: str
    payload: dict

@app.post('/sessions/{sid}/events')
async def event(sid: str, body: Event):
    state=current(sid)
    if body.kind not in ('playback_interrupted','device_error','focus_exit','student_note','capture_stats','client_diagnostic'):
        raise HTTPException(422,'不支援的事件')
    if len(json.dumps(body.payload))>10000: raise HTTPException(413,'事件過大')
    store.event(state['student_id'],sid,body.kind,body.payload)
    if sid in audits:audits[sid].record('lesson_event',event_kind=body.kind,payload=body.payload)
    return {'saved':True}

@app.get('/students/{student_id}/history')
def history(student_id: str):
    try: return {**store.history(student_id),'learning_context':store.learning_context(student_id),
                 'learning_profile':store.learning_profile(student_id),
                 'learning_evidence':store.learning_evidence(student_id)}
    except ValueError as e: raise HTTPException(404,str(e))

class SessionContextReview(BaseModel):
    include: bool
    reason: str = Field(default='',max_length=300)

@app.put('/students/{student_id}/sessions/{session_id}/context')
def review_session_context(student_id: str,session_id: str,body: SessionContextReview):
    try:
        store.set_session_context(student_id,session_id,body.include,body.reason)
        refresh_learning_memory(student_id)
        return {'saved':True,'include':body.include}
    except ValueError as e:raise HTTPException(404,str(e))

def refresh_learning_memory(student_id):
    if active and active['student_id']==student_id and bridge:
        bridge.runtime.session.long_term_memory=store.learning_profile(student_id)
        bridge.runtime.session.memory=store.learning_context(student_id)
        bridge.runtime.session.plan_revision+=1

class LearningOverride(BaseModel):
    subject: str = Field(min_length=1,max_length=40)
    label: str = Field(min_length=1,max_length=120)
    status: Literal['unassessed','practicing','needs_review','parent_confirmed']
    note: str = Field(default='',max_length=500)

@app.put('/students/{student_id}/learning/{code}')
def update_learning(student_id: str,code: str,body: LearningOverride):
    try:
        store.set_learning_override(student_id,code,body.subject,body.label,body.status,body.note)
        refresh_learning_memory(student_id)
    except ValueError as e:raise HTTPException(422,str(e))
    return {'saved':True}

class EvidenceCorrection(BaseModel):
    quote: str = Field(min_length=1,max_length=500)

@app.patch('/students/{student_id}/learning/evidence/{evidence_id}')
def correct_evidence(student_id: str,evidence_id: str,body: EvidenceCorrection):
    try:
        store.correct_learning_evidence(student_id,evidence_id,body.quote)
        refresh_learning_memory(student_id)
    except ValueError as e:raise HTTPException(404,str(e))
    return {'saved':True}

@app.delete('/students/{student_id}/learning/evidence/{evidence_id}')
def exclude_evidence(student_id: str,evidence_id: str):
    try:
        store.correct_learning_evidence(student_id,evidence_id,exclude=True)
        refresh_learning_memory(student_id)
    except ValueError as e:raise HTTPException(404,str(e))
    return {'excluded':True}

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
    try:
        with (ROOT / 'bootstrap.json').open('x', encoding='utf-8') as file:
            file.write(source.read_text(encoding='utf-8'))
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
    try: do_clear_student_history(student_id)
    except ValueError as e: raise HTTPException(404,str(e))
    return {'deleted':True}

class KeyConfig(BaseModel):
    GROQ_API_KEY: str = Field(default='',max_length=300)
    OPENAI_API_KEY: str = Field(default='',max_length=300)
    GEMINI_API_KEY: str = Field(default='',max_length=300)

@app.post('/credentials')
def credentials(body: KeyConfig):
    keys.update(body.model_dump())
    return {'saved':True}

@app.get('/credentials/status')
async def credentials_status():
    async def check_groq():
        key = api_key('GROQ_API_KEY')
        if not key:
            return {'has_key': False, 'valid': None}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get('https://api.groq.com/openai/v1/models',
                                     headers={'Authorization': f'Bearer {key}'})
                return {'has_key': True, 'valid': r.status_code == 200}
        except Exception:
            return {'has_key': True, 'valid': None}

    async def check_openai():
        key = api_key('OPENAI_API_KEY')
        if not key:
            return {'has_key': False, 'valid': None}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get('https://api.openai.com/v1/models',
                                     headers={'Authorization': f'Bearer {key}'})
                return {'has_key': True, 'valid': r.status_code == 200}
        except Exception:
            return {'has_key': True, 'valid': None}

    async def check_gemini():
        key = api_key('GEMINI_API_KEY')
        if not key:
            return {'has_key': False, 'valid': None}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    'https://generativelanguage.googleapis.com/v1beta/models',
                    headers={'x-goog-api-key': key})
                return {'has_key': True, 'valid': r.status_code == 200}
        except Exception:
            return {'has_key': True, 'valid': None}

    groq, openai_result, gemini = await asyncio.gather(
        check_groq(), check_openai(), check_gemini())
    return {'GROQ_API_KEY': groq, 'OPENAI_API_KEY': openai_result, 'GEMINI_API_KEY': gemini}

@app.post('/transcribe')
async def transcribe(file: UploadFile = File(...)):
    key=api_key('GROQ_API_KEY')
    if not key: raise HTTPException(409,'請先在家長設定填入 Groq API 金鑰')
    data=await file.read(4_000_001)
    if len(data)>4_000_000: raise HTTPException(413,'錄音過長')
    formats={'audio/webm':'webm','audio/mpeg':'mp3','audio/wav':'wav','audio/x-wav':'wav','audio/mp4':'m4a'}
    content_type=(file.content_type or 'audio/webm').split(';')[0]
    if content_type not in formats:raise HTTPException(422,'不支援的錄音格式')
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response=await client.post('https://api.groq.com/openai/v1/audio/transcriptions',headers={'Authorization':f'Bearer {key}'},files={'file':('speech.'+formats[content_type],data,content_type)},data={'model':'whisper-large-v3','language':'zh','response_format':'verbose_json','temperature':'0.0','prompt':'繁體中文，台灣國小課業。'})
    except httpx.TimeoutException:raise HTTPException(504,'Groq 轉錄超過 30 秒，請稍後重試')
    except httpx.HTTPError:raise HTTPException(502,'無法連線到 Groq，請檢查網路')
    if response.status_code!=200: raise HTTPException(502,f'Groq 回應錯誤 ({response.status_code})')
    res_data=response.json()
    raw_text=res_data.get('text','') if isinstance(res_data, dict) else str(res_data)
    segments=res_data.get('segments',[]) if isinstance(res_data, dict) else []
    return {'text':sanitize_transcript(raw_text, segments)}

class Speech(BaseModel):
    text: str = Field(min_length=1,max_length=1000)
    voice: str = Field(default='',max_length=60)

@app.post('/diagnostics/tts')
async def tts(body: Speech):
    # Explicit parent diagnostic only. No unattended student upload.
    async def render():
        audio=io.BytesIO();boundaries=[]
        effective_voice=resolve_voice(body.voice) if body.voice in ALLOWED_VOICES else EDGE_VOICE
        async for part in edge_tts.Communicate(body.text,effective_voice,boundary='WordBoundary').stream():
            if part['type']=='audio': audio.write(part['data'])
            elif part['type']=='WordBoundary': boundaries.append({k:part[k] for k in ('offset','duration','text')})
        return {'audio':base64.b64encode(audio.getvalue()).decode(),'boundaries':boundaries}
    try: return await asyncio.wait_for(render(),45)
    except Exception: raise HTTPException(502,'Edge TTS 暫時無法連線，請稍後再試')

if __name__=='__main__':
    if not TOKEN: raise SystemExit('TUTOR_CORE_TOKEN is required')
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
    READY_PORT = sock.getsockname()[1]
    uvicorn.Server(uvicorn.Config(app,log_level='warning',access_log=False)).run(sockets=[sock])
