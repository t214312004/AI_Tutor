import json
import sqlite3
from pathlib import Path
from .paths import curriculum_dir

DATA=curriculum_dir()

def coverage():
    manifest=DATA/'curriculum-manifest.json'
    if not manifest.exists():return {'indexed':0,'mapping_status':'not_imported'}
    data=json.loads(manifest.read_text(encoding='utf-8'))
    docs=data['documents']
    return {'indexed':sum(d['status']=='source_indexed' for d in docs),'failed':sum(d['status']=='failed' for d in docs),'pages':sum(d.get('pages',0) for d in docs),'mapping_status':data['semantic_coverage'],'imported_at':data['imported_at']}

def search(query,subject='',limit=8):
    file=DATA/'curriculum.sqlite3'
    if not file.exists():return []
    # Escape LIKE metacharacters; all values are parameterized.
    def pattern(text):return '%'+text.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
    db=sqlite3.connect(f'file:{file.as_posix()}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        rows=db.execute("SELECT s.title,s.url,s.sha256,p.page,p.text FROM pages p JOIN sources s ON p.source_id=s.id WHERE p.text LIKE ? ESCAPE '\\' AND s.title LIKE ? ESCAPE '\\' LIMIT ?",(pattern(query),pattern(subject),limit)).fetchall()
        results=[]
        for row in rows:
            data=dict(row);start=max(0,data['text'].find(query)-160);data['excerpt']=data.pop('text')[start:start+1200];results.append(data)
        return results
    finally:db.close()
