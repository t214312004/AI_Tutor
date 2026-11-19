"""Local summaries and evidence projection for current and historical lessons."""

from .transcript_projection import projected_records

def records(snapshot):
    allowed={'student_utterance','playback','text_reply','lesson_state',
             'learning_summary','camera_observation','student_note','interaction_policy','speaker_context'}
    result=projected_records(snapshot['events'])
    for event in snapshot['events']:
        if event['kind'] in allowed:
            if event['kind']=='student_utterance' and not event['payload'].get('text'):continue
            if event['kind']=='learning_summary':
                result.append({'id':event['id'],'kind':'learning_summary','at':event['created'],
                               'data':{'tasks':event['payload'].get('tasks',[]),
                                       'confirmed':event['payload'].get('confirmed',False)}})
                continue
            result.append({'id':event['id'],'kind':event['kind'],'at':event['created'],
                           'data':event['payload']})
    result.extend({'id':e['id'],'kind':'learning_evidence','at':e['created'],'data':e}
                  for e in snapshot['evidence'])
    if result:
        session=snapshot['session']
        result.append({'id':'session:'+session['id'],'kind':'session_context','at':session['started'],
                       'data':{'grade':session['grade'],'mode':session['mode'],'notes':session['notes']}})
    result.sort(key=lambda item:item['at'])
    return result

def student_questions(events):
    other_speakers = ('parent', 'onsite_teacher')
    question_entries=[(e['created'], idx, e['payload']['text'])
                      for idx, e in enumerate(events)
                      if e['kind']=='student_utterance' and e['payload'].get('text')
                      and e['payload'].get('speaker') not in other_speakers]
    offset=len(events)
    question_entries.extend(
        (item['at'], offset + idx, item['data']['text'])
        for idx, item in enumerate(projected_records(events))
        if item['kind']=='live_transcript' and item['data'].get('role')=='user'
        and item['data'].get('speaker') not in other_speakers and item['data'].get('text'))
    question_entries.sort(key=lambda entry:(entry[0], entry[1]))
    return [text for _, _, text in question_entries[-5:]]


def memory_payload(snapshot):
    events = snapshot['events']
    legacy=next((e['payload'] for e in reversed(events) if e['kind']=='learning_summary'), {})
    state=next((e['payload'] for e in reversed(events) if e['kind']=='lesson_state'), legacy)
    heard=[e['payload']['heard'] for e in snapshot['events']
           if e['kind']=='playback' and e['payload'].get('heard')][-5:]
    return {'tasks':state.get('tasks',[]),'confirmed':state.get('confirmed',False),
            'student_questions':student_questions(events),'heard_guidance':heard,
            'analysis':None,'provider':'local','summary_turns':0,
            'basis':'依本次已保存紀錄整理。未記錄的作答與影像不列入判斷。'}
