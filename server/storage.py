"""Local registry and physically separate student databases. No provider credentials here."""
import json
import hashlib
import os
import re
import sqlite3
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timezone

def now():
    return datetime.now(timezone.utc).isoformat()

class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type,exc_value,traceback)
        finally:
            self.close()

class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        seed_file = Path(os.environ['TUTOR_BOOTSTRAP_FILE']) if os.environ.get('TUTOR_BOOTSTRAP_FILE') else root / 'bootstrap.json'
        seed = json.loads(seed_file.read_text(encoding='utf-8')) if seed_file.exists() else {}
        self.validate_seed(seed)
        with self.registry() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS schools (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                context_json TEXT NOT NULL DEFAULT '{}')''')
            school_columns = {row['name'] for row in db.execute('PRAGMA table_info(schools)')}
            if 'context_json' not in school_columns:
                db.execute("ALTER TABLE schools ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}'")
            for school in seed.get('schools', []):
                db.execute('INSERT OR IGNORE INTO schools(id,name,context_json) VALUES (?,?,?)',
                           (school['id'], school['school'], json.dumps(school, ensure_ascii=False)))
            db.execute('''CREATE TABLE IF NOT EXISTS students (
                id TEXT PRIMARY KEY, name TEXT, grade INTEGER,
                preferences TEXT NOT NULL DEFAULT "",
                school_id TEXT REFERENCES schools(id))''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(students)')}
            if 'school_id' not in columns:
                db.execute('ALTER TABLE students ADD COLUMN school_id TEXT REFERENCES schools(id)')
                for student in seed.get('students', []):
                    db.execute('UPDATE students SET school_id=? WHERE id=?',
                               (student.get('school_id'), student['id']))
            for student in seed.get('students', []):
                db.execute('INSERT OR IGNORE INTO students(id,name,grade,preferences,school_id) VALUES (?,?,?,?,?)',
                           (student['id'], student['name'], student['grade'],
                            student.get('preferences', ''), student.get('school_id')))
        # A stopped desktop must not leave a session looking active on restart.
        for student in self.students():
            with self.student(student['id']) as db:
                db.execute("""UPDATE sessions SET status='finalizing'
                              WHERE status='active' AND id IN
                              (SELECT session_id FROM session_finalizations WHERE state IN
                               ('requested','stopping','summarizing','saving'))""")
                db.execute("UPDATE sessions SET status='interrupted', ended=? WHERE status='active'", (now(),))

    @staticmethod
    def validate_seed(seed):
        for school in seed.get('schools', []):
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', school['id']) or not school['school'].strip():
                raise ValueError('學校設定無效')
        for student in seed.get('students', []):
            if (not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', student['id']) or
                    not student['name'].strip() or not 1 <= student['grade'] <= 12):
                raise ValueError('學生設定無效')

    def add_student(self, name, grade, preferences='', school_name=''):
        if not name.strip() or not 1 <= grade <= 12:
            raise ValueError('請提供稱呼與 1–12 年級')
        sid = uuid4().hex
        with self.registry() as db:
            db.execute('INSERT INTO students(id,name,grade,preferences,school_id) VALUES (?,?,?,?,NULL)',
                       (sid, name.strip(), grade, preferences))
        self.save_profile(sid, name.strip(), preferences, grade, school_name)
        return next(s for s in self.students() if s['id'] == sid)

    def connect(self, path):
        db = sqlite3.connect(path, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def registry(self):
        return self.connect(self.root / 'registry.sqlite3')

    def students(self):
        with self.registry() as db:
            return [dict(row) for row in db.execute('''
                SELECT students.*, schools.name AS school_name
                FROM students LEFT JOIN schools ON schools.id=students.school_id
                ORDER BY CASE WHEN students.id='student-test' THEN 1 ELSE 0 END, students.grade''')]

    def schools(self):
        with self.registry() as db:
            return [dict(row) for row in db.execute('SELECT id,name,context_json FROM schools ORDER BY name')]

    def save_profile(self, student_id, name, preferences, grade, school_name):
        with self.registry() as db:
            if not db.execute('SELECT 1 FROM students WHERE id=?', (student_id,)).fetchone():
                raise ValueError('學生不存在')
            school_id = None
            if school_name:
                school = db.execute('SELECT id FROM schools WHERE name=?', (school_name,)).fetchone()
                if school:
                    school_id = school['id']
                else:
                    school_id = uuid4().hex
                    db.execute('INSERT INTO schools(id,name) VALUES (?,?)', (school_id, school_name))
            if grade is None:
                db.execute('UPDATE students SET name=?, preferences=?, school_id=? WHERE id=?',
                           (name, preferences, school_id, student_id))
            else:
                db.execute('UPDATE students SET name=?, preferences=?, grade=?, school_id=? WHERE id=?',
                           (name, preferences, grade, school_id, student_id))

    def student(self, student_id):
        if student_id not in [s['id'] for s in self.students()]:
            raise ValueError('學生不存在')
        folder = self.root / student_id
        folder.mkdir(exist_ok=True)
        db = self.connect(folder / 'learning.sqlite3')
        db.executescript('''
          CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, started TEXT, ended TEXT, status TEXT, mode TEXT, notes TEXT, grade INTEGER);
          CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, session_id TEXT REFERENCES sessions(id), kind TEXT, payload TEXT, created TEXT);
          CREATE TABLE IF NOT EXISTS learning_evidence(
            id TEXT PRIMARY KEY, session_id TEXT REFERENCES sessions(id), code TEXT NOT NULL,
            subject TEXT NOT NULL, label TEXT NOT NULL, kind TEXT NOT NULL,
            quote TEXT NOT NULL, origin TEXT NOT NULL, source_page INTEGER,
            created TEXT NOT NULL, valid INTEGER NOT NULL DEFAULT 1);
          CREATE INDEX IF NOT EXISTS learning_evidence_code ON learning_evidence(code,valid,created);
          CREATE TABLE IF NOT EXISTS learning_overrides(
            code TEXT PRIMARY KEY, subject TEXT NOT NULL, label TEXT NOT NULL,
            status TEXT NOT NULL, note TEXT NOT NULL, updated TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS learning_edits(
            id TEXT PRIMARY KEY, evidence_id TEXT NOT NULL, before_text TEXT,
            after_text TEXT, action TEXT NOT NULL, changed TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS session_finalizations(
            session_id TEXT PRIMARY KEY REFERENCES sessions(id), state TEXT NOT NULL,
            stage TEXT NOT NULL, online INTEGER NOT NULL, backend TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 0, snapshot TEXT, summary TEXT,
            error TEXT, updated TEXT NOT NULL, completed TEXT,
            dismissed INTEGER NOT NULL DEFAULT 0,
            source_hash TEXT, memory_revision INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS session_reviews(
            session_id TEXT PRIMARY KEY REFERENCES sessions(id),
            include_in_context INTEGER NOT NULL DEFAULT 1,
            reason TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL);
          PRAGMA user_version=2;
        ''')
        columns={row['name'] for row in db.execute('PRAGMA table_info(session_finalizations)')}
        if 'source_hash' not in columns:db.execute('ALTER TABLE session_finalizations ADD COLUMN source_hash TEXT')
        if 'memory_revision' not in columns:
            db.execute('ALTER TABLE session_finalizations ADD COLUMN memory_revision INTEGER NOT NULL DEFAULT 0')
        return db

    def start(self, student_id, mode, notes):
        session_id = uuid4().hex
        grade = next((s['grade'] for s in self.students() if s['id'] == student_id),None)
        if grade is None:raise ValueError('學生不存在')
        with self.student(student_id) as db:
            db.execute('INSERT INTO sessions VALUES (?,?,NULL,?,?,?,?)',(session_id,now(),'active',mode,notes,grade))
        return session_id

    def event(self, student_id, session_id, kind, payload):
        with self.student(student_id) as db:
            active = db.execute('SELECT status FROM sessions WHERE id=?',(session_id,)).fetchone()
            closing=db.execute('SELECT stage FROM session_finalizations WHERE session_id=?',(session_id,)).fetchone()
            if not active or not (active['status']=='active' or
                    active['status']=='finalizing' and closing and
                    (closing['stage']=='stopping' or
                     kind=='post_lesson_api_usage' and closing['stage'] in ('summarizing','saving'))):
                raise ValueError('此陪讀已結束')
            db.execute('INSERT INTO events VALUES(?,?,?,?,?)',(uuid4().hex,session_id,kind,json.dumps(payload,ensure_ascii=False),now()))

    def begin_finalization(self,student_id,session_id,online,backend,capture_stats=None,last_playback=None):
        with self.student(student_id) as db:
            row=db.execute('SELECT status FROM sessions WHERE id=?',(session_id,)).fetchone()
            if not row:raise ValueError('陪讀不存在')
            existing=db.execute('SELECT * FROM session_finalizations WHERE session_id=?',(session_id,)).fetchone()
            if existing:return dict(existing)
            if row['status']!='active':raise ValueError('陪讀已結束')
            if capture_stats:
                db.execute('INSERT INTO events VALUES(?,?,?,?,?)',(uuid4().hex,session_id,'capture_stats',json.dumps(capture_stats,ensure_ascii=False),now()))
            if last_playback and last_playback['heard']:
                db.execute('INSERT INTO events VALUES(?,?,?,?,?)',(uuid4().hex,session_id,'playback',
                    json.dumps({'id':last_playback['playback_id'],'heard':last_playback['heard'],
                                'complete':False,'source':'finalize_receipt'},ensure_ascii=False),now()))
            db.execute('INSERT INTO session_finalizations(session_id,state,stage,online,backend,updated) VALUES(?,?,?,?,?,?)',
                       (session_id,'requested','stopping',int(online),backend,now()))
            db.execute("UPDATE sessions SET status='finalizing' WHERE id=?",(session_id,))
            return dict(db.execute('SELECT * FROM session_finalizations WHERE session_id=?',(session_id,)).fetchone())

    def finalization(self,student_id,session_id):
        with self.student(student_id) as db:
            row=db.execute('SELECT * FROM session_finalizations WHERE session_id=?',(session_id,)).fetchone()
            return dict(row) if row else None

    def pending_finalizations(self):
        found=[]
        for student in self.students():
            with self.student(student['id']) as db:
                found.extend({'student_id':student['id'],**dict(row)} for row in db.execute(
                    "SELECT * FROM session_finalizations WHERE state IN ('requested','stopping','summarizing','saving') OR (state='failed' AND dismissed=0) ORDER BY updated"))
        return found

    def locate_finalization(self,session_id):
        for student in self.students():
            row=self.finalization(student['id'],session_id)
            if row:return student['id'],row
        return None,None

    def freeze_finalization(self,student_id,session_id,force=False):
        with self.student(student_id) as db:
            row=db.execute('SELECT snapshot FROM session_finalizations WHERE session_id=?',(session_id,)).fetchone()
            if not row:raise ValueError('找不到課後整理工作')
            if row['snapshot'] and not force:return json.loads(row['snapshot'])
            events=[{'id':r['id'],'kind':r['kind'],'payload':json.loads(r['payload']),'created':r['created']}
                    for r in db.execute('SELECT * FROM events WHERE session_id=? ORDER BY created,rowid',(session_id,))]
            edits=[dict(r) for r in db.execute('''SELECT edits.id,edits.before_text,edits.after_text,edits.action
                FROM learning_edits edits JOIN learning_evidence evidence ON evidence.id=edits.evidence_id
                WHERE evidence.session_id=? ORDER BY edits.changed,edits.rowid''',(session_id,))]
            for event in events:
                if event['kind']!='student_utterance':continue
                value=event['payload'].get('text','')
                for edit in edits:
                    if value==edit['before_text']:
                        value='' if edit['action']=='excluded' else edit['after_text']
                event['payload']['text']=value
            evidence=[dict(r) for r in db.execute('SELECT id,code,subject,label,kind,quote,origin,source_page,created FROM learning_evidence WHERE session_id=? AND valid=1 ORDER BY created',(session_id,))]
            session=dict(db.execute('SELECT * FROM sessions WHERE id=?',(session_id,)).fetchone())
            snapshot={'session':session,'events':events,'evidence':evidence,'edit_ids':[e['id'] for e in edits]}
            encoded=json.dumps(snapshot,ensure_ascii=False)
            digest=hashlib.sha256(encoded.encode('utf-8')).hexdigest()
            db.execute("UPDATE session_finalizations SET snapshot=?,source_hash=?,memory_revision=?,state='summarizing',stage='summarizing',updated=? WHERE session_id=?",
                       (encoded,digest,len(edits),now(),session_id))
            return snapshot

    def set_finalization_stage(self,student_id,session_id,stage):
        with self.student(student_id) as db:
            db.execute('UPDATE session_finalizations SET state=?,stage=?,updated=? WHERE session_id=?',
                       (stage,stage,now(),session_id))

    def complete_finalization(self,student_id,session_id,summary):
        with self.student(student_id) as db:
            saved=db.execute('SELECT snapshot,source_hash FROM session_finalizations WHERE session_id=?',(session_id,)).fetchone()
            if saved and saved['snapshot']:
                if hashlib.sha256(saved['snapshot'].encode('utf-8')).hexdigest()!=saved['source_hash']:
                    raise ValueError('摘要來源快照校驗失敗')
                snapshot=json.loads(saved['snapshot'])
                current=[(r['id'],r['quote']) for r in db.execute(
                    'SELECT id,quote FROM learning_evidence WHERE session_id=? AND valid=1 ORDER BY created',(session_id,))]
                prior=[(e['id'],e['quote']) for e in snapshot['evidence']]
                edits=[r['id'] for r in db.execute('''SELECT edits.id FROM learning_edits edits
                    JOIN learning_evidence evidence ON evidence.id=edits.evidence_id
                    WHERE evidence.session_id=? ORDER BY edits.changed,edits.rowid''',(session_id,))]
                if current!=prior or edits!=snapshot.get('edit_ids',[]):raise ValueError('學習證據已更新')
            db.execute('UPDATE session_finalizations SET state=?,stage=?,summary=?,error=NULL,updated=?,completed=? WHERE session_id=?',
                       ('completed','completed',json.dumps(summary,ensure_ascii=False) if summary else None,now(),now(),session_id))
            db.execute("UPDATE sessions SET status='ended',ended=? WHERE id=?",(now(),session_id))

    def fail_finalization(self,student_id,session_id,error):
        with self.student(student_id) as db:
            db.execute('UPDATE session_finalizations SET state=?,stage=?,error=?,updated=? WHERE session_id=?',
                       ('failed','failed',str(error)[:120],now(),session_id))
            db.execute("UPDATE sessions SET status='ended',ended=? WHERE id=?",(now(),session_id))

    def retry_finalization(self,student_id,session_id):
        with self.student(student_id) as db:
            row=db.execute('SELECT state,snapshot,error FROM session_finalizations WHERE session_id=?',(session_id,)).fetchone()
            if not row or row['state']!='failed':raise ValueError('此課後整理無法重試')
            fresh=row['error']=='學習證據已更新，請重新整理'
            stage='summarizing' if row['snapshot'] and not fresh else 'stopping'
            db.execute("UPDATE session_finalizations SET state=?,stage=?,snapshot=?,attempt=attempt+1,error=NULL,updated=? WHERE session_id=?",
                       (stage,stage,None if fresh else row['snapshot'],now(),session_id))
            db.execute("UPDATE sessions SET status='finalizing' WHERE id=?",(session_id,))
            return json.loads(row['snapshot']) if row['snapshot'] and not fresh else None

    def dismiss_finalization(self,student_id,session_id):
        with self.student(student_id) as db:
            db.execute("UPDATE session_finalizations SET dismissed=1 WHERE session_id=? AND state='failed'",(session_id,))

    def record_learning_evidence(self, student_id, session_id, item):
        if student_id == 'student-test':
            return 'test-evidence'
        if item.get('kind') not in ('asked_for_help','observed_step','repeated_revision'):
            raise ValueError('無效學習證據類別')
        if item.get('origin') not in ('student_utterance','provider_transcript','camera_observation'):
            raise ValueError('無效學習證據來源')
        values={key:str(item.get(key,'')).strip() for key in ('code','subject','label','quote')}
        if not all(values.values()) or len(values['code'])>40 or len(values['subject'])>40 or len(values['label'])>120 or len(values['quote'])>500:
            raise ValueError('學習證據內容無效')
        if not isinstance(item.get('source_page'),int) or item['source_page']<1:
            raise ValueError('缺少課綱來源頁碼')
        with self.student(student_id) as db:
            row=db.execute('SELECT status FROM sessions WHERE id=?',(session_id,)).fetchone()
            if not row or row['status']!='active':raise ValueError('此陪讀已結束')
            ident=uuid4().hex
            db.execute('INSERT INTO learning_evidence VALUES(?,?,?,?,?,?,?,?,?,?,1)',
                (ident,session_id,values['code'],values['subject'],values['label'],item['kind'],
                 values['quote'],item['origin'],item['source_page'],now()))
        return ident

    def learning_evidence(self,student_id,limit=200):
        if student_id == 'student-test':
            return []
        with self.student(student_id) as db:
            return [dict(row) for row in db.execute('''SELECT learning_evidence.*,
                    EXISTS(SELECT 1 FROM learning_edits WHERE evidence_id=learning_evidence.id
                           AND action='corrected') AS parent_corrected
                    FROM learning_evidence WHERE valid=1 ORDER BY created DESC,id DESC LIMIT ?''',(limit,))]

    def correct_learning_evidence(self,student_id,evidence_id,quote=None,exclude=False):
        if student_id == 'student-test':
            return
        if not exclude and (not isinstance(quote,str) or not quote.strip() or len(quote)>500):
            raise ValueError('修正內容須為 1–500 字')
        with self.student(student_id) as db:
            row=db.execute('SELECT quote FROM learning_evidence WHERE id=? AND valid=1',(evidence_id,)).fetchone()
            if not row:raise ValueError('找不到學習證據')
            replacement='' if exclude else quote.strip()
            db.execute('UPDATE learning_evidence SET quote=?,valid=? WHERE id=?',
                (replacement,0 if exclude else 1,evidence_id))
            db.execute('INSERT INTO learning_edits VALUES(?,?,?,?,?,?)',
                (uuid4().hex,evidence_id,row['quote'],replacement,'excluded' if exclude else 'corrected',now()))
            affected=db.execute('SELECT session_id FROM learning_evidence WHERE id=?',(evidence_id,)).fetchone()
            db.execute("""UPDATE session_finalizations
                SET state='failed',stage='failed',summary=NULL,error='學習證據已更新，請重新整理',
                    dismissed=0,updated=? WHERE session_id=? AND state='completed' AND summary IS NOT NULL""",
                (now(),affected['session_id']))

    def set_learning_override(self,student_id,code,subject,label,status,note):
        if student_id == 'student-test':
            return
        if status not in ('unassessed','practicing','needs_review','parent_confirmed'):
            raise ValueError('無效學習狀態')
        if not code or len(code)>40 or not subject or len(subject)>40 or not label or len(label)>120 or len(note)>500:
            raise ValueError('學習註記過長或缺少概念')
        with self.student(student_id) as db:
            db.execute('''INSERT INTO learning_overrides VALUES(?,?,?,?,?,?)
                ON CONFLICT(code) DO UPDATE SET subject=excluded.subject,label=excluded.label,
                status=excluded.status,note=excluded.note,updated=excluded.updated''',
                (code,subject,label,status,note.strip(),now()))

    def learning_profile(self,student_id,limit=100):
        if student_id == 'student-test':
            return []
        with self.student(student_id) as db:
            rows=db.execute('''SELECT evidence.code,evidence.subject,evidence.label,
                COUNT(*) AS count,MAX(evidence.created) AS last_seen,
                SUM(evidence.kind='asked_for_help') AS help_count,
                SUM(evidence.kind='observed_step') AS step_count,
                SUM(evidence.kind='repeated_revision') AS revision_count
                FROM learning_evidence AS evidence
                LEFT JOIN session_reviews AS review ON review.session_id=evidence.session_id
                WHERE evidence.valid=1 AND COALESCE(review.include_in_context,1)=1
                GROUP BY evidence.code ORDER BY last_seen DESC''').fetchall()
            summaries={r['code']:dict(r) for r in rows}
            for row in db.execute('SELECT * FROM learning_overrides'):
                entry=summaries.setdefault(row['code'],{'code':row['code'],'subject':row['subject'],
                    'label':row['label'],'count':0,'last_seen':row['updated'],
                    'help_count':0,'step_count':0,'revision_count':0})
                entry['parent_status']=row['status'];entry['parent_note']=row['note']
                entry['subject']=row['subject'];entry['label']=row['label']
            recent={}
            for row in db.execute('''SELECT evidence.code,evidence.kind,evidence.quote,
                    evidence.origin,evidence.created FROM learning_evidence AS evidence
                    LEFT JOIN session_reviews AS review ON review.session_id=evidence.session_id
                    WHERE evidence.valid=1 AND COALESCE(review.include_in_context,1)=1
                    ORDER BY evidence.created DESC,evidence.id DESC'''):
                bucket=recent.setdefault(row['code'],[])
                if len(bucket)<2:bucket.append(dict(row))
            result=sorted(summaries.values(),key=lambda r:r['last_seen'],reverse=True)[:limit]
            for entry in result:
                entry.setdefault('parent_status','unassessed')
                entry.setdefault('parent_note','')
                entry['basis']='觀察與求助次數不代表已掌握；只有家長可確認狀態。'
                entry['recent_evidence']=recent.get(entry['code'],[])
            return result

    def finish(self, student_id, session_id, status='ended'):
        if status not in ('ended','interrupted'):raise ValueError('無效陪讀結束狀態')
        with self.student(student_id) as db:
            db.execute("UPDATE sessions SET status=?, ended=? WHERE id=? AND status='active'",(status,now(),session_id))

    def history(self, student_id):
        with self.student(student_id) as db:
            sessions=[dict(r) for r in db.execute('''SELECT sessions.*,
                COALESCE(session_reviews.include_in_context,1) AS context_included,
                COALESCE(session_reviews.reason,'') AS context_review_reason
                FROM sessions LEFT JOIN session_reviews ON session_reviews.session_id=sessions.id
                ORDER BY sessions.started DESC LIMIT 100''')]
            events=[{**dict(r),'payload':json.loads(r['payload'])} for r in db.execute('SELECT * FROM events ORDER BY created DESC LIMIT 500')]
            corrections=[dict(r) for r in db.execute('SELECT * FROM learning_edits ORDER BY changed DESC LIMIT 500')]
            finalizations=[]
            summary_sources={}
            for row in db.execute('SELECT session_id,state,stage,summary,error,completed FROM session_finalizations ORDER BY updated DESC LIMIT 100'):
                entry=dict(row);entry['summary']=json.loads(entry['summary']) if entry['summary'] else None
                finalizations.append(entry)
                if entry['summary'] and len(summary_sources)<200:
                    snapshot_row=db.execute('SELECT snapshot FROM session_finalizations WHERE session_id=?',(entry['session_id'],)).fetchone()
                    if snapshot_row and snapshot_row['snapshot']:
                        from .post_lesson import records
                        wanted={ident for section in entry['summary'].get('analysis',{}).values()
                                for item in section for ident in item.get('sources',[])}
                        for item in records(json.loads(snapshot_row['snapshot'])):
                            if item['id'] in wanted and len(summary_sources)<200:
                                data=item['data']
                                detail=(data.get('text') or data.get('quote') or data.get('heard') or
                                        '；'.join(data.get('evidence',[])) or
                                        '；'.join(task.get('title','') for task in data.get('tasks',[])) or
                                        data.get('notes') or '')
                                summary_sources[item['id']]={'kind':item['kind'],'at':item['at'],
                                    'text':str(detail)[:350]}
        return {'sessions':sessions,'events':events,'learning_edits':corrections,
                'finalizations':finalizations,'summary_sources':summary_sources}

    def set_session_context(self,student_id,session_id,include,reason=''):
        with self.student(student_id) as db:
            session=db.execute('SELECT status FROM sessions WHERE id=?',(session_id,)).fetchone()
            if not session:raise ValueError('課程不存在')
            if session['status'] not in ('ended','interrupted'):raise ValueError('課程尚未結束')
            db.execute('''INSERT INTO session_reviews(session_id,include_in_context,reason,updated)
                          VALUES (?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET
                          include_in_context=excluded.include_in_context,
                          reason=excluded.reason,updated=excluded.updated''',
                       (session_id,1 if include else 0,reason[:300],now()))

    def learning_context(self,student_id):
        with self.student(student_id) as db:
            changes={}
            for row in db.execute('''SELECT evidence.session_id, edits.before_text,
                                           edits.after_text, edits.action
                                    FROM learning_edits AS edits
                                    JOIN learning_evidence AS evidence ON evidence.id=edits.evidence_id
                                    WHERE evidence.origin='student_utterance'
                                    ORDER BY edits.changed, edits.rowid'''):
                changes.setdefault(row['session_id'],[]).append(dict(row))
            def revised_questions(session_id,questions):
                output=[]
                for question in questions:
                    value=question
                    for change in changes.get(session_id,[]):
                        if value==change['before_text']:
                            value='' if change['action']=='excluded' else change['after_text']
                    if value:output.append(value)
                return output
            sessions=db.execute('''SELECT sessions.id,sessions.started,sessions.status,sessions.mode
                FROM sessions LEFT JOIN session_reviews ON session_reviews.session_id=sessions.id
                WHERE sessions.status IN ('ended','interrupted')
                  AND COALESCE(session_reviews.include_in_context,1)=1
                ORDER BY sessions.started DESC LIMIT 100''').fetchall()
            result=[]
            for session in sessions:
                rows=db.execute('SELECT kind,payload FROM events WHERE session_id=? ORDER BY created',(session['id'],)).fetchall()
                events=[{'kind':r['kind'],'payload':json.loads(r['payload'])} for r in rows]
                completed=db.execute("SELECT summary FROM session_finalizations WHERE session_id=? AND state='completed'",(session['id'],)).fetchone()
                summary=json.loads(completed['summary']) if completed and completed['summary'] else None
                if summary is None and session['mode']!='basic':continue
                if summary is None:summary=next((e['payload'] for e in reversed(events) if e['kind']=='learning_summary'),None)
                if summary is None:
                    state=next((e['payload'] for e in reversed(events) if e['kind']=='lesson_state'),{})
                    summary={'tasks':state.get('tasks',[]),'confirmed':state.get('confirmed',False),
                             'student_questions':[e['payload']['text'] for e in events if e['kind']=='student_utterance'][-5:],
                             'heard_guidance':[e['payload']['heard'] for e in events if e['kind']=='playback' and e['payload'].get('heard')][-5:],
                             'basis':'從已提交事件恢復，未記錄部分未知；不推定已掌握。'}
                summary={**summary,'student_questions':revised_questions(session['id'],summary.get('student_questions',[]))}
                result.append({'session_id':session['id'],'recorded_at':session['started'],'status':session['status'],**summary})
                if len(result)>=3:break
        return result
