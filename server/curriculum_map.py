"""Source-backed curriculum-code catalog and conservative lesson retrieval."""
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from .paths import curriculum_dir

DATA = curriculum_dir()
CATALOG = DATA / 'curriculum-concepts.sqlite3'
STAGES = {'I': (1, 2), 'II': (3, 4), 'III': (5, 6),
          'IV': (7, 9), 'V': (10, 12)}
ROMAN = str.maketrans({'Ⅰ': 'I', 'Ⅱ': 'II', 'Ⅲ': 'III', 'Ⅳ': 'IV', 'Ⅴ': 'V'})
CODE = re.compile(r'(?m)^[ \t*◎]{0,8}((?:[一-龥]?[A-Za-z]{1,4}|[1-9][a-z]?|1[0-2])-(?:[1-9]|1[0-2]|[ⅠⅡⅢⅣⅤ]|IV|III|II|V|I)-\d{1,3})(?=\s|$)')
WHITESPACE_RE = re.compile(r'\s+')
MATH_CONTENT_CODE_RE = re.compile(r'[NSRD]-\d{1,2}-\d{1,3}')
MATH_PERF_CODE_RE = re.compile(r'[nsrd]-[IV]+-\d{1,3}')
NON_MATH_PERF_PREFIX_RE = re.compile(r'[1-9]|[a-z]|[一-龥][1-9a-z]')
COLON_RE = re.compile(r'[：:]')
CN_ADD_RE = re.compile(r'[零一二三四五六七八九十]\s*加\s*[零一二三四五六七八九十]')
CN_SUB_RE = re.compile(r'[零一二三四五六七八九十]\s*減\s*[零一二三四五六七八九十]')
MATH_OP_RES = tuple((re.compile(r'\d\s*' + re.escape(mark) + r'\s*\d'), word)
                    for mark, word in (('+','加法'), ('＋','加法'), ('-','減法'), ('－','減法'), ('×','乘法'), ('*','乘法'), ('÷','除法')))
DATE_RE = re.compile(r'\b\d{4}-\d{2}-\d{2}(?:T[\d:.+Z-]+)?\b')
HEX_ID_RE = re.compile(r'\b[0-9a-f]{24,64}\b', flags=re.I)
ARITH_EXPR_RE = re.compile(r'\d\s*[+＋×÷*－-]\s*\d')
CN_ARITH_RE = re.compile(r'[零一二三四五六七八九十]\s*[加減乘除]\s*[零一二三四五六七八九十]')
G2_SINGLE_ADD_RE = re.compile(r'(?:[1-9]|[一二三四五六七八九])\s*(?:加|[+＋])\s*(?:[1-9]|[一二三四五六七八九])')
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
SUBJECT_NAMES = frozenset(name for name, _ in SUBJECTS)
EXPLICIT_SUBJECT_NAMES = {'數學':('數學',),'國語文':('國語','閱讀測驗','閱讀理解'),
                          '英語文':('英文','英語'),'自然科學':('自然','科學'),'社會':('社會',)}
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
    return WHITESPACE_RE.sub('', text.replace('量', '量').replace('類', '類').replace('了', '了'))

def extract(source_id, subject, page, text, metadata):
    # A code is accepted only at a line start. Cross references embedded in
    # paragraphs are intentionally not treated as separate learning items.
    matches = list(CODE.finditer(text))
    for index, match in enumerate(matches):
        raw = match.group(1)
        code = raw.translate(ROMAN)
        grades = grade_range(code)
        if not grades: continue
        if subject == '數學' and not (MATH_CONTENT_CODE_RE.fullmatch(code)
                                    or MATH_PERF_CODE_RE.fullmatch(code)): continue
        if subject != '數學' and code.split('-')[1].isdigit(): continue
        kind = ('學習內容' if (subject == '數學' and code.split('-')[1].isdigit())
                or (subject != '數學' and not NON_MATH_PERF_PREFIX_RE.match(code))
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
            separator = COLON_RE.search(first)
            if separator and 2 <= separator.start() <= 38:
                summary = first[:min(len(first), separator.start() + 210)]
                quality = 'auto_math_row'
        if not summary:
            # Keep a searchable page snippet without asserting it belongs to
            # this code. The teacher only receives matched source text.
            snippet = normalize(block[:90])
            summary = snippet if len(snippet) >= 8 else ''
        yield {'source_id': source_id, 'code': code, 'subject': subject, 'kind':kind,
               'grade_min': grades[0], 'grade_max': grades[1], 'page': page,
               'summary': summary, 'quality': quality,
               'source_sha256': metadata['sha256']}

def build(source_db=None, catalog=None):
    source_db = Path(source_db or DATA / 'curriculum.sqlite3')
    catalog = Path(catalog or CATALOG)
    catalog.parent.mkdir(parents=True, exist_ok=True)
    if source_db.resolve()==catalog.resolve():raise ValueError('來源與索引不可使用同一檔案')
    with closing(sqlite3.connect(source_db.resolve().as_uri()+'?mode=ro',uri=True)) as source, \
         closing(sqlite3.connect(catalog)) as output, output:
      # DDL participates in the transaction, preserving the previous catalog if
      # the source is incomplete or extraction fails part way through a rebuild.
      output.execute('BEGIN IMMEDIATE')
      output.execute('DROP TABLE IF EXISTS concepts')
      output.execute('''CREATE TABLE concepts(
      source_id TEXT, code TEXT, subject TEXT, kind TEXT, grade_min INTEGER, grade_max INTEGER,
      page INTEGER, summary TEXT, quality TEXT, source_sha256 TEXT,
      source_title TEXT, source_url TEXT,
      PRIMARY KEY(source_id, code, page))''')
      output.execute('CREATE INDEX IF NOT EXISTS concept_lookup ON concepts(subject, grade_min, grade_max)')
      output.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)')
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
    return {'candidate_rows': count, 'subjects_with_codes': len(subjects)}

def concepts(grade, subject, query='', limit=8, catalog=None):
    if type(grade) is not int or not 1 <= grade <= 12 or not isinstance(subject, str) or subject not in SUBJECT_NAMES: return []
    file = Path(catalog or CATALOG)
    if not file.exists(): return []
    db = sqlite3.connect(file.resolve().as_uri()+'?mode=ro', uri=True)
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
        if CN_ADD_RE.search(query) and '加法' not in words: words.append('加法')
        if CN_SUB_RE.search(query) and '減法' not in words: words.append('減法')
        for pattern, word in MATH_OP_RES:
            if pattern.search(query) and word not in words: words.append(word)
    if not words: return []
    ranked = []
    for row in rows:
        body = normalize(row['summary'])
        title = COLON_RE.split(body, 1)[0] if row['quality']=='auto_math_row' else ''
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
    # Dates and application capture IDs are not subtraction problems. Do not
    # remove ordinary student arithmetic, such as 20-6 or 10/2.
    text=DATE_RE.sub(' ',text)
    text=HEX_ID_RE.sub(' ',text)
    explicit=[s for s,terms in EXPLICIT_SUBJECT_NAMES.items() if any(term in text for term in terms)]
    if len(explicit)==1:return explicit[0]
    if len(explicit)>1:return None
    if ARITH_EXPR_RE.search(text): return '數學'
    if CN_ARITH_RE.search(text): return '數學'
    found = [subject for subject, aliases in ALIASES.items() if any(alias in text for alias in aliases)]
    return found[0] if len(found) == 1 else None

def teaching_references(grade, utterance, notes='', tasks=(), observation=None):
    current = utterance + ' ' + ' '.join(observation or [])
    subject = detect_subject(current)
    if not subject:
        task_text = ' '.join(task.get('title','') for task in tasks if task.get('status') != 'done')
        all_subjects = [s for s in (detect_subject(part) for part in (task_text, notes)) if s]
        if len(set(all_subjects)) == 1:
            subject = all_subjects[0]
    if not subject: return []
    if subject == '數學' and grade == 2 and G2_SINGLE_ADD_RE.search(current):
        prerequisite=next((r for r in concepts(1,'數學','加法',limit=8) if r['code']=='N-1-3'),None)
        if prerequisite:return [{**prerequisite,'prerequisite':True}]
    return concepts(grade,subject,current,limit=2)

def catalog_status(catalog=None):
    file = Path(catalog or CATALOG)
    if not file.exists(): return {'status':'not_built','entries':0,'subjects':0,'review':'automatic_unreviewed'}
    db = sqlite3.connect(file.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        entries, subjects = db.execute('SELECT COUNT(*),COUNT(DISTINCT subject) FROM concepts').fetchone()
        unique=db.execute('SELECT COUNT(*) FROM (SELECT DISTINCT source_id,code FROM concepts)').fetchone()[0]
        return {'status':'indexed','entries':entries,'unique_codes':unique,'subjects':subjects,'review':'automatic_unreviewed'}
    finally: db.close()
