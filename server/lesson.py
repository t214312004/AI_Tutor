"""Small, evidence-backed lesson state. No inferred grades or personality labels."""
import copy
import re

TASK_SCHEMA={'type':'object','additionalProperties':False,'properties':{
    'id':{'type':'string'},'title':{'type':'string'},
    'status':{'type':'string','enum':['todo','done']},
    'evidence_quote':{'type':'string'}},'required':['id','title','status','evidence_quote']}
LESSON_SCHEMA={'type':'object','additionalProperties':False,'properties':{
    'tasks':{'type':'array','items':TASK_SCHEMA},
    'confirmation_quote':{'type':'string'},
    'signal':{'type':'string','enum':['none','completed_step','repeated_correction']},
    'focus':{'type':'string'},'confidence':{'type':'string','enum':['low','high']}},
    'required':['tasks','confirmation_quote','signal','focus','confidence']}

def empty_lesson():
    return {'tasks':[],'confirmation_quote':'','signal':'none','focus':'','confidence':'low'}

def validate_lesson(value):
    if not isinstance(value,dict) or set(value)!=set(LESSON_SCHEMA['required']):raise ValueError('無效課程狀態')
    if value['signal'] not in ('none','completed_step','repeated_correction') or value['confidence'] not in ('low','high'):raise ValueError('無效觀察訊號')
    if any(not isinstance(value[k],str) or len(value[k])>200 for k in ('confirmation_quote','focus')):raise ValueError('課程文字過長')
    if not isinstance(value['tasks'],list) or len(value['tasks'])>12:raise ValueError('作業項目過多')
    ids=set()
    for task in value['tasks']:
        if not isinstance(task,dict) or set(task)!=set(TASK_SCHEMA['required']):raise ValueError('無效作業項目')
        if any(not isinstance(v,str) for v in task.values()):raise ValueError('無效作業文字')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}',task['id']) or task['id'] in ids:raise ValueError('重複作業識別')
        if not 1<=len(task['title'].strip())<=160 or len(task['evidence_quote'])>300 or task['status'] not in ('todo','done'):raise ValueError('無效作業狀態')
        ids.add(task['id'])
    return value

def identifies_task(quote,task,tasks):
    if len(tasks)==1 or task['title'] in quote:return True
    subjects=('數學','國語','英文','英語','自然','社會')
    named=[subject for subject in subjects if subject in quote and subject in task['title']]
    if any(sum(subject in candidate['title'] for candidate in tasks)==1 for subject in named):return True
    pages=set(re.findall(r'\d+\s*頁',quote)) & set(re.findall(r'\d+\s*頁',task['title']))
    return any(sum(page in candidate['title'] for candidate in tasks)==1 for page in pages)

def apply_lesson(session,proposal,utterance):
    """Only a fresh spoken quote or explicit UI action may confirm completion."""
    previous={t['id']:t for t in session.tasks}
    next_tasks=[]
    # Check the whole utterance. A provider may quote only "寫完了" from
    # "我還沒寫完了", which must not become proof of completion.
    completion_negated=bool(re.search('還沒|尚未|沒有|沒寫|沒做|沒完|未完成|未寫完|未做完|不是|不對|不要|嗎|呢|[?？]',utterance))
    for proposed in proposal['tasks']:
        task=copy.deepcopy(proposed);old=previous.get(task['id'])
        quote=task['evidence_quote'].strip()
        # Pictures alone must not silently mark the entire homework complete.
        supported=bool(quote and quote in utterance and not completion_negated
                       and identifies_task(quote,task,proposal['tasks'])
                       and re.search('完成|做完|寫完|好了',quote)
                       and not re.search('嗎',quote))
        task['status']='done' if task['status']=='done' and (supported or (old and old['title']==task['title'] and old['status']=='done')) else 'todo'
        if task['id'] in session.manual_task_status:
            task['status']=session.manual_task_status[task['id']]
            task['evidence_quote']='學生在畫面上勾選' if task['status']=='done' else ''
        next_tasks.append(task)
    if not next_tasks:return False
    changed=[(t['id'],t['title']) for t in next_tasks]!=[(t['id'],t['title']) for t in session.tasks]
    session.tasks=next_tasks
    if changed:session.plan_confirmed=False
    quote=proposal['confirmation_quote'].strip()
    confirmation_negated=bool(re.search('不是|不對|不要|不好|不行|不可以|錯了|嗎|呢|[?？]',utterance))
    if quote and quote in utterance and not confirmation_negated and re.search('對|沒錯|好|是|確認|就這些|今天|要寫',quote):session.plan_confirmed=True
    return True

def lesson_state(session):
    return {'tasks':session.tasks,'confirmed':session.plan_confirmed,
            'all_done':bool(session.tasks) and session.plan_confirmed and all(t['status']=='done' for t in session.tasks)}
