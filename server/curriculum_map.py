"""Source-backed curriculum-code catalog and conservative lesson retrieval."""
import re
import sqlite3
from pathlib import Path
from .paths import curriculum_dir

DATA = curriculum_dir()
CATALOG = DATA / 'curriculum-concepts.sqlite3'
STAGES = {'I': (1, 2), 'II': (3, 4), 'III': (5, 6),
          'IV': (7, 9), 'V': (10, 12)}
ROMAN = str.maketrans({'Ⅰ': 'I', 'Ⅱ': 'II', 'Ⅲ': 'III', 'Ⅳ': 'IV', 'Ⅴ': 'V'})
CODE = re.compile(r'(?m)^[ \t*◎]{0,8}((?:[一-龥]?[A-Za-z]{1,4}|[1-9][a-z]?|1[0-2])-(?:[1-9]|1[0-2]|[ⅠⅡⅢⅣⅤ]|IV|III|II|VI|V|I)-\d{1,3})(?=\s|$)')
SUBJECTS = (
    ('第二外國語文', ('第二外國語文',)),
    ('本土語文', ('本土語文',)), ('新住民語文', ('新住民語文',)),
    ('臺灣手語', ('臺灣手語',)),
    ('國語文', ('國語文',)), ('英語文', ('英語文',)),
    ('數學', ('數學領域',)), ('自然科學', ('自然科學領域',)),
    ('社會', ('社會領域',)), ('生活', ('生活課程',)),
    ('藝術', ('藝術領域',)), ('健康與體育', ('健康與體育領域',)),
    ('綜合活動', ('綜合活動領域',)), ('科技', ('科技領域',)),
    ('全民國防教育', ('全民國防教育',)),
)
ALIASES = {'國語文': ('國語', '閱讀', '注音', '造句', '生字', '寫作'),
           '英語文': ('英文', '英語', '單字', '拼字', '字母'),
           '數學': ('數學', '算式', '加法', '減法', '乘法', '除法', '分數', '小數', '圖形', '幾何', '角度', '進位', '退位', '位值'),
           '自然科學': ('自然', '科學', '植物', '動物', '水循環', '天氣', '電路'),
           '社會': ('社會', '地圖', '歷史', '文化', '地理'),
           '生活': ('生活課程',), '藝術': ('藝術', '美術'),
           '健康與體育': ('健康', '體育'), '綜合活動': ('綜合',),
           '科技': ('科技',)}
TOPICS = {'加法': ('加減', '加法', '加與減'), '減法': ('加減', '減法', '加與減'),
          '乘法': ('乘法', '乘除'), '除法': ('除法', '乘除'),
          '分數': ('分數',), '小數': ('小數',), '時間': ('時間', '時刻'),
          '長度': ('長度',), '角度': ('角度',), '周長': ('周長',),
          '面積': ('面積',), '位值': ('位值',), '進位': ('進位', '位值'),
          '退位': ('退位', '位值'), '容量': ('容量', '容量'),
          '重量': ('重量', '重量'), '閱讀': ('閱讀', '讀'),
          '注音': ('注音',), '造句': ('句子', '造句'), '生字': ('識字', '字'),
          '標點': ('標點',), '段落': ('段落',), '寫作': ('寫作',),
          '單字': ('字詞', '拼字'), '拼字': ('拼字', '字母'),
          '字母': ('字母',), '句型': ('句型', '句構'),
          '地圖': ('地圖',), '文化': ('文化',), '交通': ('交通',),
          '植物': ('植物',), '動物': ('動物',), '天氣': ('天氣',),
          '電路': ('電路',), '地震': ('地震',)}

def subject_of(title):
    for subject, terms in SUBJECTS:
        if any(term in title for term in terms): return subject
    return None

def grade_range(code):
    parts = code.translate(ROMAN).split('-')
    if len(parts) != 3: return None
    try:
        if parts[1].isdigit():
            grade = int(parts[1])
            return (grade, grade) if 1 <= grade <= 12 else None
        return STAGES.get(parts[1])
    except ValueError: return None

def normalize(text):
    return re.sub(r'\s+', '', text.replace('量', '量').replace('類', '類').replace('了', '了'))

def extract(source_id, subject, page, text, metadata):
    # A code is accepted only at a line start. Cross references embedded in
    # paragraphs are intentionally not treated as separate learning items.
    matches = list(CODE.finditer(text))
    for index, match in enumerate(matches):
        raw = match.group(1)
        code = raw.translate(ROMAN)
        grades = grade_range(code)
        if not grades: continue
        if subject == '數學' and not (re.fullmatch(r'[NSRD]-\d{1,2}-\d{1,3}', code)
                                    or re.fullmatch(r'[nsrd]-[IVX]+-\d{1,3}', code)): continue
        if subject != '數學' and code.split('-')[1].isdigit(): continue
        kind = ('學習內容' if (subject == '數學' and code.split('-')[1].isdigit())
                or (subject != '數學' and not re.match(r'[1-9]|[a-z]|[一-龥][1-9a-z]',code))
                else '學習表現')
        end = matches[index + 1].start() if index + 1 < len(matches) else min(len(text), match.end() + 600)
        block = text[match.end():end]
        # Math PDF has code + description in the same row. Other PDFs often
        # place entire columns of codes before their descriptions: no inferred
        # per-code description in that case.
        summary = ''
        quality = 'code_only'
        if subject == '數學' and kind == '學習內容':
            first = normalize(block[:260]).strip(' ：:;；，,')
            separator = re.search(r'[：:]', first)
            if separator and 2 <= separator.start() <= 38:
                summary = first[:min(len(first), separator.start() + 210)]
                quality = 'auto_math_row'
        if not summary:
            # Keep a searchable page snippet without asserting it belongs to
            # this code. The teacher only receives matched source text.
            summary = normalize(block[:90]) if len(normalize(block[:90])) >= 8 else ''
        yield {'source_id': source_id, 'code': code, 'subject': subject, 'kind':kind,
               'grade_min': grades[0], 'grade_max': grades[1], 'page': page,
               'summary': summary, 'quality': quality,
               'source_sha256': metadata['sha256']}

def build(source_db=None, catalog=None):
    source_db = Path(source_db or DATA / 'curriculum.sqlite3')
    catalog = Path(catalog or CATALOG)
    catalog.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(source_db)
    output = sqlite3.connect(catalog)
    output.executescript('''DROP TABLE IF EXISTS concepts;
      CREATE TABLE concepts(
      source_id TEXT, code TEXT, subject TEXT, kind TEXT, grade_min INTEGER, grade_max INTEGER,
      page INTEGER, summary TEXT, quality TEXT, source_sha256 TEXT,
      source_title TEXT, source_url TEXT,
      PRIMARY KEY(source_id, code, page));
      CREATE INDEX IF NOT EXISTS concept_lookup ON concepts(subject, grade_min, grade_max);
      CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);''')
    with output:
        output.execute('DELETE FROM concepts')
        output.execute('DELETE FROM meta')
        rows = source.execute('SELECT s.id,s.title,s.url,s.sha256,p.page,p.text FROM sources s JOIN pages p ON p.source_id=s.id ORDER BY s.id,p.page')
        count = 0
        subjects = set()
        for source_id, title, url, sha, page, text in rows:
            subject = subject_of(title)
            if not subject: continue
            subjects.add(subject)
            for item in extract(source_id, subject, page, text, {'sha256': sha}):
                output.execute('INSERT OR IGNORE INTO concepts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (source_id,item['code'],subject,item['kind'],item['grade_min'],item['grade_max'],page,
                     item['summary'],item['quality'],sha,title,url))
                count += 1
        output.execute('INSERT INTO meta VALUES(?,?)', ('schema_version','1'))
        output.execute('INSERT INTO meta VALUES(?,?)', ('source_count',str(source.execute('SELECT COUNT(*) FROM sources').fetchone()[0])))
    source.close();output.close()
    return {'candidate_rows': count, 'subjects_with_codes': len(subjects)}

def concepts(grade, subject, query='', limit=8, catalog=None):
    if not isinstance(grade, int) or not 1 <= grade <= 12 or not isinstance(subject, str) or subject not in dict(SUBJECTS): return []
    file = Path(catalog or CATALOG)
    if not file.exists(): return []
    db = sqlite3.connect(f'file:{file.as_posix()}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in db.execute('''SELECT code,subject,kind,grade_min,grade_max,page,summary,quality,
              source_title,source_url,source_sha256 FROM concepts
              WHERE subject=? AND grade_min<=? AND grade_max>=?
              ORDER BY page LIMIT 3000''',
              (subject,grade,grade))]
    finally: db.close()
    if not query: return rows[:limit]
    words = [word for word in TOPICS if word in query and subject in ('數學','國語文','英語文','自然科學','社會')]
    if subject == '數學':
        if re.search(r'[零一二三四五六七八九十]\s*加\s*[零一二三四五六七八九十]',query) and '加法' not in words: words.append('加法')
        if re.search(r'[零一二三四五六七八九十]\s*減\s*[零一二三四五六七八九十]',query) and '減法' not in words: words.append('減法')
        for mark, word in (('+','加法'), ('＋','加法'), ('-','減法'), ('－','減法'), ('×','乘法'), ('*','乘法'), ('÷','除法')):
            if re.search(r'\d\s*' + re.escape(mark) + r'\s*\d',query) and word not in words: words.append(word)
    if not words: return []
    ranked = []
    for row in rows:
        body = normalize(row['summary'])
        title = body.split('：',1)[0] if row['quality']=='auto_math_row' else ''
        score = sum(7 if word in title else 5 if any(form in title for form in TOPICS[word])
                    else 3 if word in body else 1 if any(form in body for form in TOPICS[word]) else 0
                    for word in words)
        if score:
            ranked.append((score, row['quality']=='auto_math_row', row))
    ranked.sort(key=lambda item:(-item[0],-item[1],item[2]['kind']!='學習內容',item[2]['page'],item[2]['code']))
    # One item per code; the same code may appear again in appendices.
    selected = []
    seen = set()
    for _,_,row in ranked:
        if row['code'] not in seen:
            selected.append(row);seen.add(row['code'])
            if len(selected) == limit: break
    return selected

def detect_subject(text):
    if re.search(r'\d\s*[+＋×÷*－-]\s*\d', text): return '數學'
    if re.search(r'[零一二三四五六七八九十]\s*[加減乘除]\s*[零一二三四五六七八九十]',text): return '數學'
    found = [subject for subject, aliases in ALIASES.items() if any(alias in text for alias in aliases)]
    # Topic words such as 分數 can help identify math; generic words such as
    # 時間 alone cannot override another explicit school subject.
    explicit_names = {'數學':('數學',),'國語文':('國語',),'英語文':('英文','英語'),
                      '自然科學':('自然','科學'),'社會':('社會',)}
    explicit = [s for s in found if any(term in text for term in explicit_names.get(s,()))]
    if len(explicit) == 1: return explicit[0]
    return found[0] if len(found) == 1 else None

def teaching_references(grade, utterance, notes='', tasks=(), observation=None):
    current = utterance + ' ' + ' '.join(observation or [])
    subject = detect_subject(current)
    if not subject:
        task_text = ' '.join(task.get('title','') for task in tasks if task.get('status') != 'done')
        candidates = [detect_subject(part) for part in (task_text, notes)]
        subject = candidates[0] if candidates[0] and candidates[0] == candidates[1] else None
        if not subject:
            all_subjects = [s for s in candidates if s]
            if len(set(all_subjects)) == 1: subject = all_subjects[0]
    if not subject: return []
    if subject == '數學' and grade == 2 and re.search(r'(?:[1-9]|[一二三四五六七八九])\s*(?:加|[+＋])\s*(?:[1-9]|[一二三四五六七八九])',current):
        prerequisite=next((r for r in concepts(1,'數學','加法',limit=8) if r['code']=='N-1-3'),None)
        if prerequisite:return [{**prerequisite,'prerequisite':True}]
    return concepts(grade,subject,current,limit=2)

def catalog_status(catalog=None):
    file = Path(catalog or CATALOG)
    if not file.exists(): return {'status':'not_built','entries':0,'subjects':0,'review':'automatic_unreviewed'}
    db = sqlite3.connect(f'file:{file.as_posix()}?mode=ro',uri=True)
    try:
        entries, subjects = db.execute('SELECT COUNT(*),COUNT(DISTINCT subject) FROM concepts').fetchone()
        unique=db.execute('SELECT COUNT(*) FROM (SELECT DISTINCT source_id,code FROM concepts)').fetchone()[0]
        return {'status':'indexed','entries':entries,'unique_codes':unique,'subjects':subjects,'review':'automatic_unreviewed'}
    finally: db.close()
