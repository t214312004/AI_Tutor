"""Bounded, app-owned learning context shared by the native voice adapters."""
def excerpt(value, limit=500):
    return value[:limit] if isinstance(value,str) else value


def learning_context(session):
    summaries = [{key:excerpt(value,300) for key,value in item.items()
                  if key in ('session_id','recorded_at','confirmed','basis')}
                 | {'tasks':[{key:excerpt(value,160) for key,value in task.items()} for task in item.get('tasks',[])[:8]],
                    'student_questions':[excerpt(x,300) for x in item.get('student_questions',[])[-3:]],
                    'heard_guidance':[excerpt(x,300) for x in item.get('heard_guidance',[])[-2:]]}
                 for item in session.memory[:3]]
    profiles = [{key:excerpt(value) for key,value in item.items() if key in
                 ('code','subject','label','help_count','step_count','revision_count','parent_status','parent_note','basis')}
                | {'recent_evidence':[{key:excerpt(value,200) for key,value in evidence.items() if key in ('kind','quote','origin','created')}
                                     for evidence in item.get('recent_evidence',[])[:2]]}
                for item in session.long_term_memory[:12]]
    return {
        'student': session.student_name, 'grade': session.grade,
        'teacher': session.teacher_name, 'notes': session.notes[:8000],
        'memory': summaries,
        'long_term_memory': profiles,
        'practice_checks': {'enabled': session.practice_checks_enabled,
            'recent_attempts': [{key:excerpt(value) for key,value in item.items()} for item in session.learning_attempts[:8]],
            'basis': '只在家長啟用且孩子同意時邀請短檢查；待核對不算成效，無紀錄不算答錯。elapsed_days 以紀錄建立時間計算，家長補記可能不等於實際作答日期。'},
        'recent_dialogue': [{key:excerpt(value) for key,value in item.items()}
                            for item in (session.native_history or session.heard_history)[-12:]],
        'context_scope':'最近三堂摘要、十二個概念與八筆已核對練習的節錄，不是完整學習檔案。',
    }
