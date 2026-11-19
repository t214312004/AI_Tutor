"""Exclude explicitly reviewed records, with a consistent backup and revision trail.

The private manifest contains student_id and records [{id, quote_sha256}].
Without --apply this only previews counts and hashes; never prints student quotes.
"""
import argparse
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server.collaboration_cli import atomic_text,safe_path
from server.data_lock import DataLock
from server.storage import EVIDENCE_UPDATED_ERROR


def repair(data_root,manifest,backup_root,apply=False):
    student=manifest['student_id']
    if not isinstance(student,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', student):
        raise ValueError('Invalid student')
    data_root=Path(data_root).resolve();dbfile=safe_path(data_root,student+'/learning.sqlite3')
    if not dbfile.is_file():raise ValueError('Database missing')
    entries=manifest['records']
    if not entries or len(entries)>50 or len({r['id'] for r in entries})!=len(entries):raise ValueError('Invalid repair manifest')
    lock = DataLock(data_root) if apply else None
    db=None
    backup=None;notes=[]
    replaced_notes=[]
    try:
        db=sqlite3.connect(dbfile.as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True)
        db.row_factory=sqlite3.Row
        def validate():
            if db.execute("SELECT 1 FROM sessions WHERE status='active'").fetchone():raise ValueError('Lesson is active; repair postponed')
            rows=[]
            for expected in entries:
                row=db.execute('SELECT * FROM learning_evidence WHERE id=?',(expected['id'],)).fetchone()
                if not row or hashlib.sha256(row['quote'].encode('utf-8')).hexdigest()!=expected['quote_sha256']:
                    raise ValueError('Reviewed source has changed; repair canceled')
                if row['valid']:rows.append(dict(row))
            return rows
        changed=validate()
        if not apply or not changed:return {'reviewed':len(entries),'excluded':0,'pending':len(changed),'applied':False}
        backup=Path(backup_root).resolve()/('repair-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8])
        backup.mkdir(parents=True)
        with closing(sqlite3.connect(backup/'learning.sqlite3')) as copy:db.backup(copy)
        runtime=safe_path(data_root,'runtime/'+student+'/collaboration')
        for role in ('teacher','vision'):
            path=safe_path(runtime,role+'-notes.md')
            if path.exists():
                shutil.copyfile(path,backup/(role+'-notes.md'));notes.append(path)
        db.execute('BEGIN IMMEDIATE')
        changed=validate()
        stamp=datetime.now(timezone.utc).isoformat()
        for row in changed:
            db.execute('UPDATE learning_evidence SET valid=0 WHERE id=?',(row['id'],))
            db.execute('INSERT INTO learning_edits VALUES(?,?,?,?,?,?)',
                (uuid4().hex,row['id'],row['quote'],row['quote'],'excluded',stamp))
        for sid in {r['session_id'] for r in changed}:
            db.execute("""UPDATE session_finalizations SET state='failed',stage='failed',summary=NULL,
                error=?,dismissed=0,updated=?,memory_revision=memory_revision+1
                WHERE session_id=? AND state='completed'""",(EVIDENCE_UPDATED_ERROR,stamp,sid))
            db.execute('INSERT INTO events VALUES(?,?,?,?,?)',(uuid4().hex,sid,'memory_revision',
                json.dumps({'reason':'reviewed_evidence_excluded','records':[r['id'] for r in changed if r['session_id']==sid]}),stamp))
        atomic_text(backup/'revision.json',json.dumps({'manifest':manifest,'original_records':changed,
            'changed':stamp,'action':'excluded','quotes_preserved':True},ensure_ascii=False))
        for path in notes:
            atomic_text(path,'# 學習記憶已修訂\n已撤回誤標的學習證據。請以程式提供的修訂後記憶為準，舊自由筆記已封存，不沿用其中的概念歸屬。\n')
            replaced_notes.append(path)
        db.commit()
        return {'reviewed':len(entries),'excluded':len(changed),'pending':0,'applied':True,'backup':str(backup)}
    except BaseException:
        if db:db.rollback()
        if backup:
            for path in replaced_notes:
                atomic_text(path, (backup/path.name).read_text(encoding='utf-8'))
            (backup/'revision.json').unlink(missing_ok=True)
        raise
    finally:
        if db:db.close()
        if lock:lock.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--data-root',type=Path,required=True)
    parser.add_argument('--manifest',type=Path,required=True);parser.add_argument('--backup-root',type=Path,required=True)
    parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    print(json.dumps(repair(args.data_root,json.loads(args.manifest.read_text(encoding='utf-8')),args.backup_root,args.apply),ensure_ascii=False))
