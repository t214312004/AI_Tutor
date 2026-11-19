"""Sourced practice records, separate from task completion and mastery overrides."""
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from uuid import uuid4

from .teaching_cards import CARDS

ASSISTANCE = ('none', 'read_prompt', 'hint', 'worked_example', 'full_solution')
PHASES = ('initial', 'practice', 'immediate_check', 'delayed_check')
RESULTS = ('unreviewed', 'correct', 'incorrect', 'not_observed')
ATTEMPT_FIELDS = ('subject', 'concept', 'question', 'response', 'assistance', 'phase', 'previous_attempt_id', 'answer_basis')
VALID_CARD_IDS = frozenset(c['id'] for c in CARDS)


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def validate_attempt(value):
    if not isinstance(value, dict) or set(value) != set(ATTEMPT_FIELDS):
        raise ValueError('練習紀錄欄位無效')
    if any(not isinstance(v, str) for v in value.values()):
        raise ValueError('練習紀錄必須使用文字')
    limits = {'subject':40, 'concept':120, 'question':1000, 'response':1000,
              'previous_attempt_id':40, 'answer_basis':1000}
    if any(len(value[key]) > limit for key, limit in limits.items()):
        raise ValueError('練習紀錄過長')
    cleaned = {key: text.strip() for key, text in value.items()}
    if any(not cleaned[key] for key in ('subject','concept','question')):
        raise ValueError('請提供科目、概念與題目')
    if cleaned['assistance'] not in ASSISTANCE or cleaned['phase'] not in PHASES:
        raise ValueError('協助程度或檢查階段無效')
    if cleaned['phase'] == 'delayed_check' and not cleaned['previous_attempt_id']:
        raise ValueError('延後檢查須連結先前練習')
    return cleaned


class PracticeStoreMixin:
    @staticmethod
    def practice_schema(db):
        db.executescript('''
            CREATE TABLE IF NOT EXISTS practice_settings(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS learning_attempts(
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, payload TEXT NOT NULL,
                origin TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE, created TEXT NOT NULL,
                result TEXT NOT NULL DEFAULT 'unreviewed', review_note TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT, revision INTEGER NOT NULL DEFAULT 1, valid INTEGER NOT NULL DEFAULT 1);
            CREATE INDEX IF NOT EXISTS learning_attempt_session ON learning_attempts(session_id,created);
            CREATE TABLE IF NOT EXISTS learning_attempt_edits(
                id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, before_text TEXT NOT NULL,
                after_text TEXT NOT NULL, changed TEXT NOT NULL);
        ''')

    def practice_settings(self, student_id):
        with self.student(student_id) as db:
            row = db.execute('SELECT payload FROM practice_settings WHERE id=1').fetchone()
        return json.loads(row['payload']) if row else {'enabled':False, 'reviewed_cards':[]}

    def save_practice_settings(self, student_id, enabled, reviewed_cards):
        if (type(enabled) is not bool or not isinstance(reviewed_cards, list)
                or any(not isinstance(x, str) or x not in VALID_CARD_IDS for x in reviewed_cards)):
            raise ValueError('短檢查設定無效')
        value = {'enabled':enabled, 'reviewed_cards':sorted(set(reviewed_cards))}
        with self.student(student_id) as db:
            db.execute('INSERT INTO practice_settings VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                       (json.dumps(value),))
        return value

    def record_learning_attempt(self, student_id, session_id, value, origin='parent', source_id=''):
        item = validate_attempt(value)
        if origin not in ('parent', 'typed_text', 'provider_transcript') or not isinstance(source_id,str) or len(source_id)>160:
            raise ValueError('練習來源無效')
        if origin != 'parent' and (not source_id or not item['response']):
            raise ValueError('AI 紀錄缺少可追溯的作答')
        if student_id == 'student-test':
            return None
        with self.student(student_id) as db:
            session = db.execute('SELECT status FROM sessions WHERE id=?',(session_id,)).fetchone()
            if not session or session['status'] == 'finalizing' or origin != 'parent' and session['status'] != 'active':
                raise ValueError('課程不存在、已結束或正在整理')
            if origin != 'parent':
                settings = db.execute('SELECT payload FROM practice_settings WHERE id=1').fetchone()
                if not settings or not json.loads(settings['payload'])['enabled']:
                    raise ValueError('家長尚未啟用短檢查紀錄')
            if item['previous_attempt_id']:
                prior = db.execute('SELECT * FROM learning_attempts WHERE id=? AND valid=1',(item['previous_attempt_id'],)).fetchone()
                if not prior:
                    raise ValueError('找不到同一學生的先前練習')
                old = json.loads(prior['payload'])
                if (old['subject'], old['concept']) != (item['subject'], item['concept']):
                    raise ValueError('延後檢查須為同一概念')
                if item['phase']=='delayed_check' and (prior['session_id']==session_id or
                        (datetime.now(timezone.utc)-datetime.fromisoformat(prior['created'])).total_seconds()<86400):
                    raise ValueError('延後檢查須跨堂且相隔至少一天')
            encoded = json.dumps(item,ensure_ascii=False,sort_keys=True)
            # A source is recorded at most once per question, even if callbacks change the proposed answer.
            key = hashlib.sha256(json.dumps([session_id,origin,source_id or uuid4().hex,
                                             item['subject'],item['concept'],item['question']],ensure_ascii=False).encode()).hexdigest()
            existing = db.execute('SELECT id FROM learning_attempts WHERE source_key=?',(key,)).fetchone()
            if existing:return existing['id']
            ident = uuid4().hex
            try:
                db.execute('INSERT INTO learning_attempts(id,session_id,payload,origin,source_key,created) VALUES(?,?,?,?,?,?)',
                           (ident,session_id,encoded,origin,key,timestamp()))
            except sqlite3.IntegrityError:
                existing = db.execute('SELECT id FROM learning_attempts WHERE source_key=?',(key,)).fetchone()
                if existing:return existing['id']
                raise
        return ident

    def learning_attempts(self, student_id, context_only=False, limit=100):
        if student_id == 'student-test':return []
        with self.student(student_id) as db:
            context_filter = " AND COALESCE(r.include_in_context,1)=1 AND a.reviewed_at IS NOT NULL AND a.result!='unreviewed'" if context_only else ''
            rows = db.execute('''SELECT a.*, COALESCE(r.include_in_context,1) AS context_included,
                    p.created AS prior_created
                FROM learning_attempts a JOIN sessions s ON s.id=a.session_id
                LEFT JOIN session_reviews r ON r.session_id=a.session_id
                LEFT JOIN learning_attempts p ON p.id=json_extract(a.payload, '$.previous_attempt_id') AND p.valid=1
                WHERE a.valid=1''' + context_filter + ' ORDER BY a.created DESC,a.rowid DESC LIMIT ?',(8 if context_only else limit,)).fetchall()
            entries = [{**dict(r), **json.loads(r['payload'])} for r in rows]
        for entry in entries:
            prior_created = entry.pop('prior_created', None)
            entry.pop('payload');entry.pop('source_key')
            entry['independent_verified'] = bool(entry['reviewed_at'] and entry['result']=='correct' and entry['assistance']=='none')
            entry['elapsed_days'] = (
                (datetime.fromisoformat(entry['created'])-datetime.fromisoformat(prior_created)).total_seconds()/86400
                if entry['previous_attempt_id'] and prior_created else None)
        return entries

    def review_learning_attempt(self, student_id, attempt_id, value, result, note, revision, exclude=False):
        item = validate_attempt(value)
        if result not in RESULTS or not isinstance(note,str) or len(note)>1000 or type(revision) is not int:
            raise ValueError('練習複核資料無效')
        if result in ('correct','incorrect') and (not item['response'] or not item['answer_basis']):
            raise ValueError('判定對錯須保留作答及核對依據')
        with self.student(student_id) as db:
            old = db.execute('SELECT * FROM learning_attempts WHERE id=? AND valid=1',(attempt_id,)).fetchone()
            if not old:raise ValueError('找不到練習紀錄')
            if old['revision']!=revision:raise ValueError('紀錄已更新，請重新載入')
            # Identity and timing stay anchored. Correct the text/assistance, never silently relink a delayed check.
            original = json.loads(old['payload'])
            if any(item[k]!=original[k] for k in ('subject','concept','phase','previous_attempt_id')):
                raise ValueError('科目、概念與檢查關聯不可改寫，請排除後重建')
            stamp = timestamp();encoded=json.dumps(item,ensure_ascii=False,sort_keys=True)
            updated = db.execute(
                'UPDATE learning_attempts SET payload=?,result=?,review_note=?,reviewed_at=?,revision=revision+1,valid=? '
                'WHERE id=? AND valid=1 AND revision=?',
                (encoded,result,note,stamp,0 if exclude else 1,attempt_id,revision)).rowcount
            if updated != 1:raise ValueError('紀錄已更新，請重新載入')
            db.execute('INSERT INTO learning_attempt_edits VALUES(?,?,?,?,?)',
                       (uuid4().hex,attempt_id,json.dumps(dict(old),ensure_ascii=False),
                        json.dumps({'payload':item,'result':result,'note':note,'excluded':exclude},ensure_ascii=False),stamp))

    def practice_edits(self, student_id):
        if student_id=='student-test':return []
        with self.student(student_id) as db:
            return [dict(row) for row in db.execute('SELECT * FROM learning_attempt_edits ORDER BY changed DESC LIMIT 500')]
