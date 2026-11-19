"""Import official NAER curriculum PDFs with source, checksum and page provenance.

This indexes official documents; it does not pretend that PDF retrieval equals a
human-validated grade-by-grade concept graph or complete textbook collection.
"""
import asyncio
from contextlib import closing
import sys
import hashlib
import io
import json
import os
import re
import sqlite3
from datetime import datetime,timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin,urlparse
from uuid import uuid4
import httpx
from pypdf import PdfReader

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.collaboration_cli import atomic_text
from server.paths import curriculum_dir
DATA = curriculum_dir()
INDEX='https://www.naer.edu.tw/PageSyllabus?fid=52'
MAX_PDF_BYTES = 30_000_000

class Links(HTMLParser):
    def __init__(self):super().__init__();self.links=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        href = a.get('href') or ''
        if tag=='a' and href.lower().endswith('.pdf') and a.get('class')!='decree-date':
            url=urljoin(INDEX,href)
            if urlparse(url).hostname=='www.naer.edu.tw':self.links.append({'url':url,'title':re.sub(r'\.[pP][dD][fF].*','',a.get('title') or '')})

async def main():
    target=DATA/'pdfs';target.mkdir(parents=True,exist_ok=True)
    fetched=datetime.now(timezone.utc).isoformat();documents=[]
    async with httpx.AsyncClient(timeout=60,follow_redirects=True) as client:
        r=await client.get(INDEX);r.raise_for_status();first=r.text
        csrf=re.search(r'name="csrf" value="([^"]+)"',first)
        if not csrf:raise RuntimeError('Official catalog form changed; inspect before importing')
        second=await client.post(INDEX,data={'csrf':csrf[1],'sid':'177','tid':'179'});second.raise_for_status()
        parser=Links();parser.feed(first);parser.feed(second.text)
        unique={d['url']:d for d in parser.links};gate=asyncio.Semaphore(3)
        def extract_pages(raw_bytes):
            reader=PdfReader(io.BytesIO(raw_bytes))
            return [{'page':number,'text':page.extract_text() or ''} for number,page in enumerate(reader.pages,1)]
        async def download(doc):
            async with gate:
                source_id=hashlib.sha256(doc['url'].encode()).hexdigest()[:20];file=target/(source_id+'.pdf')
                try:
                    if file.exists():
                        if file.stat().st_size > MAX_PDF_BYTES:raise ValueError('Invalid cached PDF')
                        data=await asyncio.to_thread(file.read_bytes)
                        if not data.startswith(b'%PDF') or len(data)>MAX_PDF_BYTES:raise ValueError('Invalid cached PDF')
                    else:
                        data = bytearray()
                        async with client.stream('GET', doc['url']) as response:
                            response.raise_for_status()
                            async for chunk in response.aiter_bytes(128 * 1024):
                                if len(data) + len(chunk) > MAX_PDF_BYTES:raise ValueError('PDF response too large')
                                data.extend(chunk)
                        if not data.startswith(b'%PDF'):raise ValueError('Invalid PDF response')
                        tmp=file.with_name(f'.{source_id}-{uuid4().hex}.tmp')
                        try:
                            await asyncio.to_thread(tmp.write_bytes,data)
                            os.replace(tmp,file)
                        finally:tmp.unlink(missing_ok=True)
                    pages=await asyncio.to_thread(extract_pages,data)
                    document={**doc,'id':source_id,'sha256':hashlib.sha256(data).hexdigest(),'retrieved_at':fetched,'pages':len(pages),'text_pages':sum(bool(p['text'].strip()) for p in pages),'status':'source_indexed','mapping_status':'not_reviewed'}
                    print(json.dumps({'title':doc['title'],'pages':len(pages)},ensure_ascii=False),flush=True)
                    return document,pages
                except Exception as e:return {**doc,'id':source_id,'status':'failed','error':type(e).__name__},[]
        results=await asyncio.gather(*(download(doc) for doc in unique.values()))
    database=DATA/'curriculum.sqlite3'
    with closing(sqlite3.connect(database)) as db:
        db.executescript('CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,title TEXT,url TEXT,sha256 TEXT,metadata TEXT); CREATE TABLE IF NOT EXISTS pages(source_id TEXT,page INTEGER,text TEXT,PRIMARY KEY(source_id,page));')
        with db:
            for doc,pages in results:
                documents.append(doc)
                if doc['status']!='source_indexed':continue
                db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?)',(doc['id'],doc['title'],doc['url'],doc['sha256'],json.dumps(doc,ensure_ascii=False)))
                db.execute('DELETE FROM pages WHERE source_id=?',(doc['id'],))
                db.executemany('INSERT INTO pages VALUES(?,?,?)',[(doc['id'],p['page'],p['text']) for p in pages])
    manifest={'official_index':INDEX,'catalog_scope':'總綱、國民中小學暨普通型高中領域／科目課程綱要','imported_at':fetched,'semantic_coverage':'not_reviewed','documents':documents}
    atomic_text(DATA/'curriculum-manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    from server.curriculum_map import build
    catalog = build()
    print(json.dumps({'indexed':sum(d['status']=='source_indexed' for d in documents),'failed':sum(d['status']=='failed' for d in documents),'database':str(database)}))
    print(json.dumps({'concept_catalog': catalog},ensure_ascii=False))

if __name__=='__main__':asyncio.run(main())
