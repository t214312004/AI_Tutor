"""Synthetic fixtures installed explicitly by tests, never production defaults."""
from pathlib import Path
import shutil
import json
import sqlite3
from .storage import Store

DEMO = Path(__file__).resolve().parents[1] / 'examples' / 'demo.json'


def install_test_seed(root):
    file = Path(root) / 'bootstrap.json'
    if not file.exists():
        shutil.copyfile(DEMO, file)


class SeededStore(Store):
    def __init__(self, root):
        install_test_seed(root)
        super().__init__(root)


def seed_curriculum():
    """Build isolated synthetic catalog; never read the author's curriculum."""
    from .paths import curriculum_dir
    from .curriculum_map import build
    target = curriculum_dir()
    target.mkdir(parents=True, exist_ok=True)
    source = target / 'curriculum.sqlite3'
    with sqlite3.connect(source) as db:
        db.executescript('CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,title TEXT,url TEXT,sha256 TEXT,metadata TEXT); CREATE TABLE IF NOT EXISTS pages(source_id TEXT,page INTEGER,text TEXT,PRIMARY KEY(source_id,page));')
        metadata = {'sha256': '0' * 64}
        db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?)',
                   ('synthetic', '數學領域合成測試資料', 'https://example.invalid/synthetic', '0' * 64, json.dumps(metadata)))
        db.execute('INSERT OR REPLACE INTO pages VALUES(?,?,?)',
                   ('synthetic', 1, 'N-2-2 乘法：合成練習\nN-4-5 乘法與分數：合成練習'))
    build(source, target / 'curriculum-concepts.sqlite3')
    (target / 'curriculum-manifest.json').write_text(json.dumps({
        'documents': [{'status': 'source_indexed', 'pages': 1}],
        'semantic_coverage': 'synthetic_fixture', 'imported_at': 'synthetic',
    }), encoding='utf-8')
