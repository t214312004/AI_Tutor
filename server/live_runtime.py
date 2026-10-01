"""Live audio, CLI teaching tools and a bounded, session-owned provider lifetime."""
import asyncio
import base64
import json
import re
import time
from uuid import uuid4

import aiohttp
from .basic import BasicRuntime
from .live import GPTLive, GeminiLive
from .teaching_policy import TUTOR_RULES
from .lesson import lesson_state
from .curriculum_map import teaching_references, catalog_status


def chunks(text, limit=480):
    part=''
    for char in text:
        if len((part+char).encode('utf-8'))>limit:
            yield part;part=''
        part+=char
    if part:yield part


class LiveRuntime(BasicRuntime):
    def __init__(self,session,teacher,observer,mode,key,emit,persist,gpt_config=None):
        super().__init__(session,teacher,observer,None,emit,persist)
        self.mode=mode
        self.gpt_config=gpt_config
        context=json.dumps({'年級':session.grade,'本次注意事項':session.notes},ensure_ascii=False)
        if mode=='gpt':
            backend_rules=TUTOR_RULES.format(teacher_name=session.teacher_name or '老師').replace(
                '輸出符合指定 JSON schema。有 quiet 欄位且為 true 時 speech 必須是空字串。','')
            backend_rules+='\n需要理解作業或學生歷史時呼叫 get_lesson_context；需要 108 課綱內容時呼叫 get_curriculum_references；問題涉及照片時呼叫 inspect_photo。只依工具回傳的照片觀察描述畫面。課綱片段是自動擷取、尚未逐條人工審核的適齡教學參考，不是學生課本或實際進度。可以用 set_board 或 close_board 控制白板。'
            backend_rules+='\n開始陪讀確認範圍時，主動取得 get_lesson_context 與可用照片證據，不要求孩子先指定工具。需要核對照片而既有觀察不足時呼叫 inspect_photo(fresh=false)；若沒有可用照片或需要重拍，再呼叫 inspect_photo(fresh=true)。工具已由應用程式配置，不需搜尋環境來發現能力。收到照片不代表必須朗讀；只在當下教學需要時使用證據。'
            self.provider=GPTLive(key,instructions=context,teacher_name=session.teacher_name,
                delegation=gpt_config['delegation'] if gpt_config else None,
                backend_instructions=backend_rules+'\n學生背景：'+context)
        else:self.provider=GeminiLive(key,instructions=context,teacher_name=session.teacher_name)
        self.http=None;self.reader=None;self.writer=None
        self.input_queue=asyncio.Queue(maxsize=40)
        self.ready_event=asyncio.Event();self.final_event=asyncio.Event()
        self.failure=None;self.closing=False;self.close_lock=asyncio.Lock();self.report=None
        self.tools={};self.transcripts={'user':'','assistant':''};self.last_role=None
        self.latest_user_request=''
        self.client_opening_attempted=False
        self.transcript_id=None;self.transcript_entry=None
        self.last_recorded_help=''
        self.response_calls={};self.response_completed=set();self.response_usage=[];self.response_usage_ids=set()
        self.response_ids={}
        self.delegation_generations={}
        self.response_io_lock=asyncio.Lock();self.response_tools_inflight=0
        self.pending_response_texts=[]

    @property
    def responses_mode(self):
        return self.mode=='gpt' and self.gpt_config is not None and self.gpt_config['delegation']['type']=='responses'

    async def start(self):
        self.http=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None,sock_connect=12))
        try:
            await self.provider.connect(self.http)
            self.reader=asyncio.create_task(self.read_provider())
            await asyncio.wait_for(self.ready_event.wait(),18)
            if self.failure:raise RuntimeError(self.failure)
            self.writer=asyncio.create_task(self.write_provider())
        except BaseException:
            await self.close();raise

    async def write_provider(self):
        try:
            while not self.closing:
                kind,value,epoch=await self.input_queue.get()
                if kind=='audio':
                    if self.mic_enabled and epoch==self.mic_epoch:await self.provider.send_audio(value)
                elif kind=='mic':await self.provider.mute(not value)
        except asyncio.CancelledError:raise
        except Exception:
            self.mic_enabled=False
            await self.error('Live 音訊傳送失敗，請結束後重新連線。')
            await self.send({'type':'media_stopped'})
            await self.provider.release()

    def enqueue(self,kind,value,epoch=0):
        if self.closing:return
        try:self.input_queue.put_nowait((kind,value,epoch))
        except asyncio.QueueFull:raise ValueError('Live 上傳不及，連線已停止；請重新開始。')

    def accept_audio(self,pcm):
        if self.session.active and self.mic_enabled:self.enqueue('audio',pcm,self.mic_epoch)

    def set_mic(self,enabled):
        super().set_mic(enabled);self.enqueue('mic',enabled)

    def accept_text(self,text):
        if self.mode!='gpt':return super().accept_text(text)
        if not isinstance(text,str) or not 1<=len(text.strip())<=4000:raise ValueError('請輸入 1–4000 字')
        if not self.session.active:return
        if len(self.voice_tasks)>=3:raise ValueError('請稍候老師回覆')
        self.latest_user_request=text.strip()
        self.transcripts['user']=self.latest_user_request
        self.last_role=None
        self.activity(student_turn=True)
        self.launch(self.gpt_text(text.strip()))

    async def gpt_text(self,text):
        self.session.heard_history.append({'role':'user','text':text})
        self.persist('student_utterance',{'text':text,'source':'text'})
        await self.send({'type':'transcript','role':'user','text':text})
        if self.responses_mode:
            async with self.response_io_lock:
                if self.response_calls or self.response_tools_inflight:
                    self.pending_response_texts.append(text)
                else:
                    await self.provider.response_text(text)
        else:
            result=await self.decision(text)
            if result and result['speech']:
                for part in chunks(result['speech']):await self.provider.append(part,speak=True)

    async def read_provider(self):
        try:
            async for event in self.provider.events():
                if self.provider.ready:self.ready_event.set()
                if self.mode=='gpt':await self.gpt_event(event)
                else:await self.gemini_event(event)
            if not self.closing and not self.final_event.is_set():
                raise RuntimeError('Live 連線已中斷')
        except asyncio.CancelledError:raise
        except Exception:
            self.failure='Live 連線失敗，請確認金鑰、模型權限與網路。'
            self.ready_event.set()
            if not self.closing:
                await self.error(self.failure)
                await self.send({'type':'media_stopped'})
                self.mic_enabled=False
                await self.provider.release()
        finally:self.final_event.set()

    async def transcript(self,role,text):
        if not text or self.closing:return
        if self.last_role!=role:
            self.transcripts[role]=''
            self.transcript_id=uuid4().hex
            self.transcript_entry={'role':role,'text':'','status':'provider_transcript_not_playback_receipt'}
            self.session.heard_history.append(self.transcript_entry)
        self.last_role=role;self.transcripts[role]=(self.transcripts[role]+text)[-6000:]
        if role=='user':self.latest_user_request=self.transcripts[role]
        self.transcript_entry['text']=self.transcripts[role]
        self.persist('live_transcript',{'role':role,'text':text,'transcript_id':self.transcript_id,
                                        'playback_verified':False})
        await self.send({'type':'transcript','role':role,'text':self.transcripts[role],
                         'transcript_id':self.transcript_id})

    async def gpt_event(self,event):
        kind=event.get('type')
        if kind=='session.output_audio.delta' and not self.closing:
            await self.send({'type':'pcm','audio':event['delta'],'rate':24000})
        elif kind=='session.input_transcript.delta':await self.transcript('user',event.get('delta',''))
        elif kind=='session.output_transcript.delta':await self.transcript('assistant',event.get('delta',''))
        elif kind=='session.delegation.created':
            delegation=event.get('delegation',{})
            if self.audit:self.audit.record('live_tool_call',provider=self.mode,call=delegation)
            if delegation.get('target')=='responses' and delegation.get('id'):
                self.delegation_generations[delegation['id']]=self.session.generation
                if delegation.get('response_id'):
                    self.response_ids[delegation['id']]=delegation['response_id']
            if delegation.get('target')=='client' and not self.responses_mode:
                self.spawn_tool(delegation['id'],self.delegate(delegation['id']))
        elif kind=='response.event' and self.responses_mode:
            await self.responses_event(event)
        elif kind=='error':raise RuntimeError('GPT Live rejected an event')
        elif kind=='session.closed':
            self.final_event.set()
            if not self.closing:
                self.mic_enabled=False
                await self.send({'type':'media_stopped'})
                await self.error('Live 服務已結束此連線，請結束後重新開始。')

    async def responses_event(self,envelope):
        event=envelope.get('event',{})
        kind=event.get('type');delegation_id=envelope.get('delegation_id')
        if kind=='response.created' and delegation_id:
            response_id=event.get('response',{}).get('id')
            if response_id:self.response_ids[delegation_id]=response_id
        elif kind=='response.output_item.done':
            item=event.get('item',{})
            if item.get('type')=='function_call' and delegation_id:
                self.response_calls.setdefault(delegation_id,[]).append(item)
        elif kind=='response.completed':
            response=event.get('response',{})
            response_id=response.get('id') or self.response_ids.get(delegation_id) or delegation_id
            if response.get('usage') and response_id not in self.response_usage_ids:
                self.response_usage_ids.add(response_id)
                usage={'provider':'openai','role':'teaching','model':self.gpt_config['delegation']['model'],
                       'effort':self.gpt_config['delegation']['effort'],'usage':response['usage']}
                self.response_usage.append(usage)
                if self.audit:self.audit.record('gpt_responses_usage',**usage)
            calls=self.response_calls.pop(delegation_id,[])
            if calls and response_id not in self.response_completed:
                self.response_completed.add(response_id)
                ticket=self.delegation_generations.get(delegation_id,self.session.generation)
                self.response_tools_inflight+=1
                work=self.run_responses_tools(calls,ticket)
                if not self.spawn_tool('responses-'+str(response_id),work):
                    if self.closing:self.response_tools_inflight-=1
                    else:await self.run_responses_tools(calls,ticket)
        elif kind in ('response.failed','response.incomplete'):
            self.response_calls.pop(delegation_id,None)
            async with self.response_io_lock:
                if not self.response_calls and not self.response_tools_inflight:
                    for text in self.pending_response_texts:
                        await self.provider.response_user_item(text)
                    if self.pending_response_texts:await self.provider.response_continue()
                    self.pending_response_texts.clear()
            await self.error('教學委派未完成，請再說一次。')

    async def run_responses_tools(self,calls,ticket):
        outputs_complete=False
        try:
            for call in calls:
                try:
                    arguments=json.loads(call.get('arguments') or '{}')
                    if not isinstance(arguments,dict):raise ValueError('Invalid tool arguments')
                    result=({'cancelled':True} if ticket!=self.session.generation else
                            await self.responses_tool(call.get('name'),arguments,ticket))
                    if ticket!=self.session.generation:result={'cancelled':True}
                except asyncio.CancelledError:raise
                except Exception as error:
                    result={'error':type(error).__name__,'message':'工具暫時無法完成'}
                await self.provider.response_tool_output(call['call_id'],result)
                if self.audit:self.audit.record('live_tool_result',provider='gpt_responses',
                                                call_id=call['call_id'],result=result)
            outputs_complete=True
        finally:
            async with self.response_io_lock:
                self.response_tools_inflight-=1
                if outputs_complete:
                    pending=self.pending_response_texts
                    self.pending_response_texts=[]
                    for text in pending:await self.provider.response_user_item(text)
                    await self.provider.response_continue()
                else:self.pending_response_texts.clear()

    def curriculum_context(self,topic):
        if not isinstance(topic,str) or not 1<=len(topic.strip())<=500:
            return {'status':'invalid_topic','references':[]}
        observation=self.session.observation
        evidence=(observation['evidence'] if self.camera_enabled and observation and
                  time.monotonic()-observation['received']<=30 else [])
        references=teaching_references(self.session.grade,topic.strip(),self.session.notes,
                                       self.session.tasks,evidence)
        return {'status':catalog_status()['status'],'review':'automatic_unreviewed',
                'references':[{'code':item['code'],'subject':item['subject'],
                               'kind':item['kind'],'grade_range':[item['grade_min'],item['grade_max']],
                               'excerpt':item['summary'][:220],'source_title':item['source_title'],
                               'source_page':item['page'],'source_url':item['source_url'],
                               'extraction':item['quality'],'prerequisite':item.get('prerequisite',False)}
                              for item in references]}

    async def responses_tool(self,name,args,ticket=None):
        if ticket is not None and ticket!=self.session.generation:return {'cancelled':True}
        if name=='get_lesson_context':
            observation=self.session.observation
            if not self.camera_enabled or not observation or time.monotonic()-observation['received']>30:
                observation=None
            return {'grade':self.session.grade,'student_name':self.session.student_name,
                    'notes':self.session.notes[:4000],'lesson':lesson_state(self.session),
                    'recent_turns':self.session.heard_history[-12:],
                    'prior_lesson_context':self.session.memory[:5],
                    'learning_notes':self.session.long_term_memory[:5],
                    'curriculum':self.curriculum_context(self.latest_user_request)
                        if self.latest_user_request else None,
                    'photo':{'capture_id':observation['capture_id'],'evidence':observation['evidence'],
                             'captured_at':observation['captured_at']} if observation else None}
        if name=='get_curriculum_references':
            return self.curriculum_context(args.get('topic'))
        if name=='inspect_photo':
            frame=await self.capture_now() if args.get('fresh') else self.frame
            if ticket is not None and ticket!=self.session.generation:return {'cancelled':True}
            if (not frame or not self.camera_enabled or frame['camera_epoch']!=self.camera_epoch
                    or time.monotonic()-frame['received']>20):
                return {'status':'unavailable','message':'目前沒有可用的新照片'}
            observation=self.session.observation
            if not observation or observation['capture_id']!=frame['id']:
                self.observations.cancel_current()
                await super().observe(frame)
                observation=self.session.observation
            if ticket is not None and ticket!=self.session.generation:return {'cancelled':True}
            if not observation or observation['capture_id']!=frame['id']:
                return {'status':'unavailable','message':'照片辨識未完成或已過期'}
            return {'status':'observed','capture_id':frame['id'],'captured_at':frame['captured_at'],
                    'evidence':observation['evidence']}
        if name=='set_board':
            html=args.get('html')
            if not isinstance(html,str) or not 1<=len(html)<=100_000:
                return {'error':'invalid_board'}
            if ticket is not None and ticket!=self.session.generation:return {'cancelled':True}
            self.board_revision+=1
            await self.send({'type':'board','html':html,'close':False,'revision':self.board_revision})
            return {'shown':True,'revision':self.board_revision}
        if name=='close_board':
            if ticket is not None and ticket!=self.session.generation:return {'cancelled':True}
            self.board_revision+=1
            await self.send({'type':'board','html':'','close':True,'revision':self.board_revision})
            return {'closed':True,'revision':self.board_revision}
        return {'error':'unknown_tool'}

    async def gemini_event(self,event):
        content=event.get('serverContent',{})
        if content.get('interrupted'):
            await super().interrupt({})
            await self.send({'type':'audio_clear'})
        for part in content.get('modelTurn',{}).get('parts',[]):
            audio=part.get('inlineData',{})
            if audio.get('mimeType','').startswith('audio/pcm') and not self.closing:
                await self.send({'type':'pcm','audio':audio['data'],'rate':24000})
        if 'inputTranscription' in content:await self.transcript('user',content['inputTranscription'].get('text',''))
        if 'outputTranscription' in content:await self.transcript('assistant',content['outputTranscription'].get('text',''))
        for call in event.get('toolCall',{}).get('functionCalls',[]):
            if self.audit:self.audit.record('live_tool_call',provider=self.mode,call=call)
            self.spawn_tool(call['id'],self.gemini_tool(call))
        for ident in event.get('toolCallCancellation',{}).get('ids',[]):
            if self.audit:self.audit.record('live_tool_cancelled',provider=self.mode,call_id=ident)
            if ident in self.tools:self.tools[ident].cancel()
        if 'goAway' in event:
            await self.error('Gemini 即將結束此連線，請結束後重新開始陪讀。')

    def spawn_tool(self,ident,work):
        if self.closing or ident in self.tools or len(self.tools)>=3:
            work.close();return False
        async def run():
            try:await work
            except asyncio.CancelledError:raise
            except Exception:await self.error('教學工具暫時失敗，請再說一次需要的協助。')
            finally:self.tools.pop(ident,None)
        self.tools[ident]=asyncio.create_task(run())
        return True

    async def send_live_image(self,frame):
        if not self.provider.ready:return False
        await self.provider.send_image(base64.b64decode(frame['image'].split(',',1)[1]))
        if self.audit:self.audit.record('live_image_sent',capture_id=frame['id'],provider=self.mode)
        return True

    async def decision(self,request):
        ticket=self.session.generation
        frame=(self.frame if self.mode=='gpt' and not self.gpt_config and self.camera_enabled
               and self.frame and time.monotonic()-self.frame['received']<20 else None)
        result=await self.teacher.respond(self.session,request,image_url=frame['image'] if frame else None)
        if result and result['need_capture']:
            frame=await self.capture_now()
            if frame and (not self.camera_enabled or frame['camera_epoch']!=self.camera_epoch):frame=None
            if frame:
                if self.mode=='gemini':
                    if self.closing or ticket!=self.session.generation:return None
                    return {'image_sent':True} if await self.send_live_image(frame) else None
                if self.gpt_config:
                    await super().observe(frame)
                    if self.session.observation and self.session.observation['capture_id']==frame['id']:
                        result=await self.teacher.respond(self.session,request+'\n已更新照片觀察，請依可見證據回答。')
                else:result=await self.teacher.respond(self.session,request+'\n這是即時照片。',image_url=frame['image'])
        if not result or self.closing or ticket!=self.session.generation:return None
        user_text=self.latest_user_request.strip()
        if (user_text and user_text!=self.last_recorded_help and
                re.search(r'不會|不懂|怎麼|如何|為什麼|教我|提示|幫忙|求助',user_text) and
                result.get('curriculum_candidates')):
            self.remember_help(result,user_text,origin='provider_transcript')
            self.last_recorded_help=user_text
        if result.get('curriculum_candidates'):
            self.persist('curriculum_candidates',{'items':result['curriculum_candidates']})
        if result['board_html'] or result['close_board']:
            self.board_revision+=1
            await self.send({'type':'board','html':result['board_html'],'close':result['close_board'],'revision':self.board_revision})
        return result

    async def delegate(self,ident):
        request=self.latest_user_request or '請依目前對話與作業提供下一個提示。'
        if not self.client_opening_attempted:
            self.client_opening_attempted=True
            request=('開始陪讀：請主動確認最新可用的照片觀察與本堂作業範圍，依證據確認要做哪題。'
                '若照片觀察不足，請要求拍攝新照片；看不清的部分明確說明，不猜測，也不必搜尋環境來發現看圖能力。'
                '\n同時處理學生目前的提問：'+request)
        result=await self.decision(request)
        if result:
            for text in chunks(result['speech'] or '目前請安靜陪伴，不需要開口。'):
                ready=self.provider.ready
                await self.provider.append(text,ident,speak=bool(result['speech']))
                if self.audit and ready:
                    self.audit.record('live_text_sent',provider=self.mode,text=text,
                                      delegation_id=ident,speak=bool(result['speech']))

    async def gemini_tool(self,call):
        ticket=self.session.generation
        if call['name']=='capture_now':
            frame=await self.capture_now()
            if frame and (self.closing or ticket!=self.session.generation or
                          not self.camera_enabled or frame['camera_epoch']!=self.camera_epoch):frame=None
            if frame:
                sent=await self.send_live_image(frame)
            else:sent=False
            result={'captured':sent}
        elif call['name']=='close_board':
            self.board_revision+=1
            await self.send({'type':'board','html':'','close':True,'revision':self.board_revision})
            result={'closed':True}
        elif call['name']=='tutor_help':
            decision=await self.decision(str(call.get('args',{}).get('request','請提供提示。'))[:4000])
            result=({'image_sent':True} if decision and decision.get('image_sent') else
                    {'hint':decision['speech'],'evidence':decision['evidence']} if decision else {'cancelled':True})
        else:result={'error':'unknown_tool'}
        if not self.closing and ticket==self.session.generation and self.provider.ready:
            await self.provider.tool_result(call,result)
            if self.audit:self.audit.record('live_tool_result',provider=self.mode,
                                            call_id=call['id'],result=result)

    async def observe(self,frame):
        if self.mode=='gemini':
            if (not self.closing and self.camera_enabled and frame['camera_epoch']==self.camera_epoch
                    and time.monotonic()-frame['received']<=30):
                await self.send_live_image(frame)
            return
        await super().observe(frame)
        observation=self.session.observation
        if not observation or observation['capture_id']!=frame['id'] or self.closing:return
        text='背景作業觀察，無須朗讀或打斷：'+json.dumps(observation['evidence'],ensure_ascii=False)
        for part in chunks(text):
            ready=self.provider.ready
            await self.provider.append(part)
            if self.audit and ready:self.audit.record('live_text_sent',provider=self.mode,text=part)

    async def interrupt(self,receipt=None):
        await super().interrupt(receipt)
        await self.send({'type':'audio_clear'})

    async def close(self):
        async with self.close_lock:
            if self.report is not None:return self.report
            self.closing=True;self.mic_enabled=False
            for task in list(self.tools.values()):task.cancel()
            await asyncio.gather(*list(self.tools.values()),return_exceptions=True)
            if self.writer:self.writer.cancel();await asyncio.gather(self.writer,return_exceptions=True)
            if self.mode=='gpt' and self.provider.ready:
                try:
                    await self.provider.request_close()
                    await asyncio.wait_for(self.final_event.wait(),15)
                except (TimeoutError,aiohttp.ClientError,ConnectionError):pass
            await self.provider.release()
            if self.reader:self.reader.cancel();await asyncio.gather(self.reader,return_exceptions=True)
            if self.http:await self.http.close()
            basic=await super().close()
            self.report={**self.provider.ledger.report(),'mode':self.mode,'cli':'subscription','image_skipped':basic['image_skipped'],
                         'gemini_usage':self.provider.ledger.gemini_events,
                         'gpt_responses_usage':self.response_usage,
                         'gpt_vision_usage':getattr(self.observer.provider,'usage',[]),
                         'gpt_live_config':self.gpt_config}
            return self.report
