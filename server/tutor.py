"""Provider-independent tutoring decisions. UI applies only current proposals."""
import asyncio
import json
import re
from html.parser import HTMLParser
import time
from dataclasses import dataclass, field
from uuid import uuid4
from copy import deepcopy
from .lesson import LESSON_SCHEMA,validate_lesson,lesson_state
from .curriculum_map import teaching_references,detect_subject
from .teaching_policy import TUTOR_RULES
from .work_lifecycle import CURRENT_WORK, applicable_observation

DECISION_SCHEMA={'type':'object','additionalProperties':False,'properties':{
 'quiet':{'type':'boolean'},'speech':{'type':'string'},'board_html':{'type':'string'},
 'close_board':{'type':'boolean'},'need_capture':{'type':'boolean'},
 'evidence':{'type':'array','items':{'type':'string'}}},
  'required':['quiet','speech','board_html','close_board','need_capture','evidence']}

OBSERVATION_SCHEMA={'type':'object','additionalProperties':False,'properties':{
 'evidence':{'type':'array','items':{'type':'string'}},
 'signal':{'type':'string','enum':['none','completed_step','repeated_correction']},
 'focus':{'type':'string'},'confidence':{'type':'string','enum':['low','high']}},
 'required':['evidence','signal','focus','confidence']}

BASIC_TURN_RULES='''沿用本堂課已給的學生背景與工作目錄中的教學規則，依本回合資料和指定 JSON schema 回覆。依孩子需求選擇合適教法；清楚區分已讀到的題目、學生提供的文字與不確定的推測。'''

CONTROL_RULES='''\n你主導教學節奏。interaction.mode 用 unchanged、normal、listening、closing；reason 自然說明原因。要求先聽完、正在朗讀或由現場老師指導時用 listening，不逐段評語；明確要求提示或說讀完了才能 normal；收尾用 closing。不要因沉默恢復發話。lesson 是正式畫面的作業提案，勾選必須有本次原話支持；共識文件仍以自然語言寫。learning 只提議本次確實求助的課綱概念，包含 code、quote、reason；quote 必須原文，候選課綱不等於學習證據，無法確認概念或說話者時留空。不要把日期當算式。'''

def collaboration_schema(schema):
    schema['properties'].update({
        'interaction':{'type':'object','additionalProperties':False,'properties':{
            'mode':{'type':'string','enum':['unchanged','normal','listening','closing']},
            'reason':{'type':'string'}},'required':['mode','reason']},
        'learning':{'type':'array','items':{'type':'object','additionalProperties':False,
            'properties':{key:{'type':'string'} for key in ('code','quote','reason')},
            'required':['code','quote','reason']}}})
    schema['required'].extend(key for key in ('interaction','learning') if key not in schema['required'])
    # These are application actions, not the format of collaboration messages.
    return schema

def parse_observation_response(raw):
    result=json.loads(raw)
    if not isinstance(result,dict) or set(result)!=set(OBSERVATION_SCHEMA['required']):raise ValueError('Invalid observation fields')
    if not isinstance(result['evidence'],list) or any(not isinstance(x,str) or len(x)>500 for x in result['evidence']):raise ValueError('Invalid evidence')
    if result['signal'] not in ('none','completed_step','repeated_correction') or result['confidence'] not in ('low','high'):
        raise ValueError('Invalid observation signal')
    if not isinstance(result['focus'],str) or len(result['focus'])>200:raise ValueError('Invalid observation focus')
    return {'evidence':result['evidence'],'lesson':{key:result[key] for key in ('signal','focus','confidence')}}

VISIBLE_INTERNAL_RE = re.compile(
    r'\bagents?\b|\bCLI\b|\bCodex\b|\bagy\b|\bResponses\s+API\b|協作夥伴|視覺(?:夥伴|角色|模型)|'
    r'內部(?:留言|夥伴|工具|機制|分工)|委派|工作區|(?:另一個|另一位|其他)\s*(?:模型|老師|代理)|'
    r'internal/shared|drafts/[0-9a-f]|classroom-request\.json|'
    r'tutor_help|get_lesson_context|get_photo_observation|inspect_homework_photo|inspect_photo|'
    r'update_lesson_notes|update_whiteboard|update_homework_plan|set_teaching_interaction|'
    r'record_learning_help|record_learning_attempt',
    re.I,
)

def validate_visible_text(text):
    if VISIBLE_INTERNAL_RE.search(text):
        raise ValueError('學生回覆包含內部分工')


def validate_student_output(result):
    class Visible(HTMLParser):
        def __init__(self):super().__init__();self.parts=[];self.hidden=0
        def handle_starttag(self,tag,attrs):
            if tag in ('script','style'):self.hidden+=1
            if not self.hidden:
                self.parts.extend(value for key,value in attrs if value and key in ('alt','title','aria-label','placeholder','value'))
        def handle_endtag(self,tag):
            if tag in ('script','style'):self.hidden=max(0,self.hidden-1)
        def handle_data(self,data):
            if not self.hidden:self.parts.append(data)
    board=Visible();board.feed(result['board_html'])
    visible=result.get('speech','')+'\n'+''.join(board.parts)
    visible+='\n'+'\n'.join(task['title'] for task in result.get('lesson',{}).get('tasks',[]))
    validate_visible_text(visible)


def parse_tutor_response(raw,schema):
    result=json.loads(raw)
    if not isinstance(result,dict) or not set(schema['required'])<=set(result) or not set(result)<=set(schema['properties']):raise ValueError('Invalid tutor fields')
    if 'lesson' in result:validate_lesson(result['lesson'])
    if 'interaction' in result:
        policy=result['interaction']
        if (not isinstance(policy,dict) or set(policy)!={'mode','reason'} or
                policy['mode'] not in ('unchanged','normal','listening','closing') or
                not isinstance(policy['reason'],str) or len(policy['reason'])>500):raise ValueError('Invalid interaction policy')
    if 'learning' in result:
        if not isinstance(result['learning'],list) or len(result['learning'])>3:raise ValueError('Invalid learning proposal')
        for proposal in result['learning']:
            if (not isinstance(proposal,dict) or set(proposal)!={'code','quote','reason'} or
                    any(not isinstance(v,str) or len(v)>500 for v in proposal.values())):raise ValueError('Invalid learning proposal')
    for key in ('quiet','close_board','need_capture'):
        if type(result[key]) is not bool:raise ValueError('Invalid tutor flag')
    for key in ('speech','board_html'):
        if not isinstance(result[key],str):raise ValueError('Invalid tutor text')
    if len(result['speech'])>1200 or len(result['board_html'])>100_000:raise ValueError('Tutor content too large')
    if not isinstance(result['evidence'],list) or any(not isinstance(x,str) or len(x)>500 for x in result['evidence']):raise ValueError('Invalid evidence')
    return result

@dataclass
class TutorSession:
    student_id:str
    grade:int
    notes:str=''
    id:str=field(default_factory=lambda:uuid4().hex)
    generation:int=0
    active:bool=True
    playback_receipt:dict|None=None
    heard_history:list=field(default_factory=list)
    native_history:list=field(default_factory=list)
    last_speech:float=0
    observation:dict|None=None
    camera_enabled:bool=False
    student_name:str=''
    teacher_name:str=''
    basic_workflow:bool=False
    tasks:list=field(default_factory=list)
    plan_confirmed:bool=False
    memory:list=field(default_factory=list)
    long_term_memory:list=field(default_factory=list)
    learning_attempts:list=field(default_factory=list)
    practice_checks_enabled:bool=False
    reviewed_teaching_cards:list=field(default_factory=list)
    workspace_context:dict=field(default_factory=dict)
    collaboration:object|None=None
    plan_revision:int=0
    manual_task_status:dict=field(default_factory=dict)
    work_sequence:int=0
    committed_sequence:int=0
    direction_revision:int=0
    interaction_mode:str='normal'
    interaction_note:str=''
    input_speaker:str='unknown'
    def interrupt(self, receipt):
        self.generation+=1;self.playback_receipt=receipt
    def end(self):self.active=False;self.generation+=1

class TutorEngine:
    def __init__(self,provider):
        self.provider=provider;self.lock=asyncio.Lock();self.basic_sent=None
    async def respond(self,session:TutorSession,utterance:str,image_url=None,observation=False,image_age_seconds=None,photo_notice=None,input_source='text'):
        if session.collaboration and not observation:
            image_url=None;photo_notice=None
        if session.basic_workflow:
            return await self.respond_basic(session,utterance,image_url,observation,image_age_seconds,photo_notice,input_source)
        ticket=session.generation;revision=session.plan_revision;started=time.monotonic()
        async with self.lock:
            if not session.active or ticket!=session.generation or revision!=session.plan_revision:return None
            context={'grade':session.grade,'student_name':session.student_name,'session_notes':session.notes,'student_utterance':utterance,
              'last_heard_turns':session.heard_history[-12:],'trusted_playback_status':session.playback_receipt,
              'observation_only':observation,'instruction':'觀察只回報證據；不因沉默催促。' if observation else ''}
            schema=deepcopy(DECISION_SCHEMA);rules=TUTOR_RULES.format(teacher_name=session.teacher_name or '老師')
            if session.collaboration and not observation:
                collaboration_schema(schema);schema['properties']['lesson']=LESSON_SCHEMA
                schema['required'].append('lesson')
                rules+=CONTROL_RULES
                work=CURRENT_WORK.get()
                context['last_heard_turns']=deepcopy(work.history if work else session.heard_history[-30:])
                context['lesson_state']=lesson_state(session)
                context['interaction']={'mode':session.interaction_mode,'reason':session.interaction_note,'speaker':session.input_speaker}
            if observation:
                schema['properties']['lesson']=LESSON_SCHEMA;schema['required'].append('lesson')
                rules+='''\n這是背景影像觀察，僅回報可見證據，speech 留空，不更改清單與白板。
lesson.signal 只有看到可核對的正確新步驟才用 completed_step；同一題反覆修改才用 repeated_correction。
安靜、手不動、遮擋或看不清都用 none；confidence 不確定就 low，focus 必須指出具體題目。'''
            references=[]
            if not observation:
                context['previous_sessions']=deepcopy(session.memory[:3])
                observation_evidence=applicable_observation(session) or {}
                if observation_evidence:
                    context['latest_photo_observation']={
                        'capture_id':observation_evidence.get('capture_id'),
                        'captured_at':observation_evidence.get('captured_at'),
                        'evidence':observation_evidence.get('evidence',[]),
                        'observation_text':observation_evidence.get('text','')}
                references=teaching_references(session.grade,utterance,session.notes,
                    session.tasks,observation_evidence.get('evidence',[]))
                subject=references[0]['subject'] if references else detect_subject(utterance)
                reference_codes={reference['code'] for reference in references}
                relevant=[entry for entry in session.long_term_memory if
                    entry['code'] in reference_codes or
                    (not reference_codes and subject and entry['subject']==subject)]
                if relevant:
                    context['long_term_learning']=[{
                        'code':item['code'],'concept':item['label'],
                        'help_count':item['help_count'],'observed_step_count':item['step_count'],
                        'revision_count':item['revision_count'],
                        'parent_status':item['parent_status'],
                        'parent_note':item['parent_note'][:500],
                        'recent_evidence':[{**e,'quote':e['quote'][:200]}
                                           for e in item['recent_evidence']],
                        'basis':item['basis']} for item in relevant[:5]]
                    rules+='''\n長期學習資料是家長可修正的證據摘要。求助或觀察次數不等於能力評分。parent_confirmed 表示家長確認已熟悉；needs_review 表示家長希望多練；practicing 表示仍在練習。依孩子當下需求自行決定教法，家長註記可作參考但不能覆蓋資料來源規則。不要向學生朗讀檔案內容。'''
                if references:
                    context['curriculum_references']=[{
                        'code':r['code'],'subject':r['subject'],'kind':r['kind'],
                        'grade_range':[r['grade_min'],r['grade_max']],
                        'excerpt':r['summary'][:220],
                        'source_title':r['source_title'],'source_page':r['page'],
                        'source_url':r['source_url'],'extraction':r['quality'],
                        'prerequisite':r.get('prerequisite',False)}
                        for r in references]
                    rules+='''\n課綱參考是已匯入官方 PDF 的自動擷取片段，未逐條人工審核。可用作適齡概念與教法背景；不得當成學生課本、實際進度或當前題目的標準答案。若片段與作業不合，忽略。對學生自然說明，不必朗讀課綱編碼。'''
            raw=await self.provider.turn(rules+'\n資料：'+json.dumps(context,ensure_ascii=False),image_url=image_url,schema=schema)
            if not session.active or ticket!=session.generation or revision!=session.plan_revision:return None
            if observation and time.monotonic()-started>30:return None
            result=parse_tutor_response(raw,schema)
            if result['quiet']:result['speech']=''
            # Observation proposals never directly speak, mutate whiteboard or save memory.
            if observation:
                return {**result,'quiet':True,'speech':'','board_html':'','close_board':False}
            return {**result,'generation':ticket,'session_id':session.id,
                    'curriculum_candidates':[
                        {'code':r['code'],'subject':r['subject'],
                         'label':r['summary'].split('：',1)[0][:120] or r['code'],
                         'source':r['source_title'],'page':r['page'],
                         'prerequisite':r.get('prerequisite',False)} for r in references]}

    async def respond_basic(self,session,utterance,image_url,observation,image_age_seconds,photo_notice,input_source='text'):
        ticket=session.generation;revision=session.plan_revision;started=time.monotonic()
        async with self.lock:
            if not session.active or ticket!=session.generation or revision!=session.plan_revision:return None
            if hasattr(self.provider,'ensure_conversation'):
                await self.provider.ensure_conversation()
            provider_generation=getattr(self.provider,'conversation_generation',0)
            if not session.active or ticket!=session.generation or revision!=session.plan_revision:return None
            sent=(self.basic_sent if self.basic_sent and self.basic_sent['session_id']==session.id
                  and self.basic_sent.get('provider_generation',0)==provider_generation else None)
            first=sent is None
            sent=sent or {}
            heard_end=len(session.heard_history)
            context={'turn_kind':'photo_observation' if observation else 'teaching',
                     'request':utterance if not observation else '照片已更新；需要時自行讀取，只回報可見證據。',
                     'current_photo_available':bool(photo_notice or image_url)}
            if session.collaboration and not observation:
                latest=applicable_observation(session)
                if latest:
                    context['latest_photo_observation']={'capture_id':latest['capture_id'],
                        'captured_at':latest.get('captured_at',''),'observation_text':latest.get('text',''),
                        'evidence':latest.get('evidence',[])}
                    context['current_photo_available']=True
                else:
                    context['latest_photo_observation']=None
                    context['current_photo_available']=False
            if not observation:
                context['request_source']=input_source
                if input_source=='speech_transcript':
                    context['transcript_note']='這是自動語音轉錄，可能有錯；若句意或關鍵字不合理，先向學生確認。'
            if first:
                context.update({'grade':session.grade,'student_name':session.student_name,
                                'session_notes':session.notes,
                                'workspace_continuity':deepcopy(session.workspace_context)})
            state={'lesson_state':lesson_state(session),'camera_enabled':session.camera_enabled,
                   'previous_sessions':deepcopy(session.memory),
                   'trusted_playback_status':deepcopy(session.playback_receipt)}
            for key,value in state.items():
                if first or sent.get(key)!=value:context[key]=value
            heard=deepcopy(session.heard_history[sent.get('heard_end',0):heard_end])
            if not observation and heard and heard[-1].get('role')=='user' and heard[-1].get('text')==utterance:
                heard.pop()  # The current request is already present above.
            if heard:context['new_heard_turns']=heard
            if photo_notice:
                context['selected_photo']={key:photo_notice[key] for key in
                                           ('capture_id','version','captured_at','archive_path','camera_epoch','capture_meta')
                                           if key in photo_notice}
            if photo_notice and sent.get('photo_version')!=photo_notice['version']:
                context['photo_updated']=photo_notice
            if (photo_notice or image_url) and image_age_seconds is not None:
                context['current_photo_age_seconds']=round(image_age_seconds,1)
            schema=deepcopy(OBSERVATION_SCHEMA if observation else DECISION_SCHEMA)
            if not observation:
                schema['properties']['lesson']=LESSON_SCHEMA;schema['required'].append('lesson')
                if session.collaboration:
                    collaboration_schema(schema)
                    context['interaction']={'mode':session.interaction_mode,'reason':session.interaction_note,'speaker':session.input_speaker}
            references=[];reference_payload=[];learning_payload=[]
            if not observation:
                latest=applicable_observation(session)
                references=teaching_references(session.grade,utterance,session.notes,
                    session.tasks,(latest or {}).get('evidence',[]))
                subject=references[0]['subject'] if references else detect_subject(utterance)
                reference_codes={reference['code'] for reference in references}
                relevant=[entry for entry in session.long_term_memory if
                    entry['code'] in reference_codes or
                    (not reference_codes and subject and entry['subject']==subject)]
                learning_payload=[{
                    'code':item['code'],'concept':item['label'],
                    'help_count':item['help_count'],'observed_step_count':item['step_count'],
                    'revision_count':item['revision_count'],'parent_status':item['parent_status'],
                    'parent_note':item['parent_note'][:500],
                    'recent_evidence':[{**e,'quote':e['quote'][:200]} for e in item['recent_evidence']],
                    'basis':item['basis']} for item in relevant[:5]]
                reference_payload=[{
                    'code':r['code'],'subject':r['subject'],'kind':r['kind'],
                    'grade_range':[r['grade_min'],r['grade_max']],
                    'excerpt':r['summary'][:220],'source_title':r['source_title'],
                    'source_page':r['page'],'source_url':r['source_url'],
                    'extraction':r['quality'],'prerequisite':r.get('prerequisite',False)}
                    for r in references]
                if (first or sent.get('long_term_learning')!=learning_payload) and learning_payload:
                    context['long_term_learning']=learning_payload
                elif not learning_payload and sent.get('long_term_learning'):
                    context['long_term_learning']=[]
                if (first or sent.get('curriculum_references')!=reference_payload) and reference_payload:
                    context['curriculum_references']=reference_payload
                elif not reference_payload and sent.get('curriculum_references'):
                    context['curriculum_references']=[]
            prompt=(BASIC_TURN_RULES if not observation else
                    '這是背景照片更新；你可自行決定是否讀取 selected_photo.archive_path。只依實際可見內容回報，不說話或修改作業。')
            prompt+=('\n照片由視覺夥伴確認；需要核對時使用協作工具留下自然語言問題。以已提交觀察為證據；舊文件不是目前畫面。學生端保持同一位老師。'
                     if session.collaboration else '\n若需看圖，只讀本回合 selected_photo.archive_path；未讀或看不清時不可聲稱看見。')
            request=prompt+'\n資料：'+json.dumps(context,ensure_ascii=False)
            if session.collaboration and not observation:request+=CONTROL_RULES
            raw=await self.provider.turn(request,image_url=image_url,schema=schema)
            if not session.active or ticket!=session.generation or revision!=session.plan_revision:return None
            if observation and time.monotonic()-started>30:return None
            if observation:
                result=parse_observation_response(raw)
            else:
                try:result=parse_tutor_response(raw,schema)
                except ValueError:
                    # Only malformed, unapplied text is retried. Keep the same photo and native conversation.
                    raw=await self.provider.turn(request+'\n上一個回覆未通過 JSON 格式驗證。請依相同資料重新回覆，'
                                                 '只輸出符合指定 schema 的 JSON。',image_url=image_url,schema=schema)
                    if not session.active or ticket!=session.generation or revision!=session.plan_revision:return None
                    result=parse_tutor_response(raw,schema)
            next_sent={**sent,**deepcopy(state),'session_id':session.id,'heard_end':heard_end,
                       'provider_generation':provider_generation}
            if photo_notice:next_sent['photo_version']=photo_notice['version']
            if not observation:
                next_sent['long_term_learning']=deepcopy(learning_payload)
                next_sent['curriculum_references']=deepcopy(reference_payload)
            self.basic_sent=next_sent
            if observation:return result
            if result['quiet']:result['speech']=''
            return {**result,'generation':ticket,'session_id':session.id,
                    'curriculum_candidates':[
                        {'code':r['code'],'subject':r['subject'],
                         'label':r['summary'].split('：',1)[0][:120] or r['code'],
                         'source':r['source_title'],'page':r['page'],
                         'prerequisite':r.get('prerequisite',False)} for r in references]}
