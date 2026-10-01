import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import curriculum_map
from .tutor import TutorEngine, TutorSession

class CurriculumMapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.source = Path(self.folder.name) / 'source.sqlite3'
        self.catalog = Path(self.folder.name) / 'concepts.sqlite3'
        db = sqlite3.connect(self.source)
        db.executescript('''CREATE TABLE sources(id TEXT PRIMARY KEY,title TEXT,url TEXT,sha256 TEXT,metadata TEXT);
                          CREATE TABLE pages(source_id TEXT,page INTEGER,text TEXT,PRIMARY KEY(source_id,page));''')
        db.executemany('INSERT INTO sources VALUES(?,?,?,?,?)', [
            ('math','十二年國教數學領域','https://example.test/math.pdf','test-math-sha','{}'),
            ('science','十二年國教自然科學領域','https://example.test/science.pdf','test-science-sha','{}')])
        db.executemany('INSERT INTO pages VALUES(?,?,?)', [
            ('math',22,'N-1-3\n基本加減法：以操作活動為主。\nn-I-2\n理解加法和減法。\nN-2-2\n加減算式與直式計算：用位值理解方法。\nN-4-5\n同分母分數：比較、加、減。'),
            ('science',24,'INd-Ⅱ-6\n動植物體的外部形態與生長有關。\nINd-Ⅲ-6\n其他階段的生態概念。')])
        db.commit(); db.close()
        curriculum_map.build(self.source,self.catalog)
        self.patcher = patch.object(curriculum_map,'CATALOG',self.catalog)
        self.patcher.start()
    def tearDown(self):
        self.patcher.stop()
        self.folder.cleanup()
    def test_grade_and_stage_are_source_backed(self):
        self.assertEqual(curriculum_map.subject_of('第二外國語文課程綱要'),'第二外國語文')
        math = curriculum_map.concepts(4,'數學','分數')
        self.assertEqual([r['code'] for r in math],['N-4-5'])
        self.assertEqual((math[0]['page'],math[0]['source_sha256']), (22,'test-math-sha'))
        self.assertEqual(curriculum_map.concepts(2,'數學','分數'),[])
        science = curriculum_map.concepts(4,'自然科學','植物')
        self.assertEqual([r['code'] for r in science],['INd-II-6'])
        self.assertEqual((science[0]['grade_min'],science[0]['grade_max']),(3,4))
        self.assertEqual(curriculum_map.concepts(2,'自然科學','植物'),[])
        performance=curriculum_map.concepts(2,'數學','加法')
        self.assertTrue(any(r['code']=='n-I-2' and r['kind']=='學習表現' for r in performance))
    def test_small_addition_uses_explicit_prerequisite(self):
        refs=curriculum_map.teaching_references(2,'七加五怎麼算？','今天做數學')
        self.assertEqual(refs[0]['code'],'N-1-3')
        self.assertTrue(refs[0]['prerequisite'])
        self.assertEqual(curriculum_map.teaching_references(2,'這題怎麼做？','國語和數學都有'),[])
    async def test_tutor_receives_grade_limited_source_without_claiming_textbook(self):
        class Recorder:
            def __init__(self): self.prompt=''
            async def turn(self,text,**kwargs):
                self.prompt=text
                return json.dumps({'quiet':False,'speech':'先找共同的分母。',
                                   'board_html':'','close_board':False,'need_capture':False,'evidence':[]})
        recorder=Recorder()
        session=TutorSession('grade4',4,'今天做數學')
        result=await TutorEngine(recorder).respond(session,'分數怎麼比較？')
        self.assertEqual(result['speech'],'先找共同的分母。')
        self.assertIn('N-4-5',recorder.prompt)
        self.assertIn('source_page',recorder.prompt)
        self.assertIn('不得當成學生課本',recorder.prompt)
        self.assertNotIn('N-1-3',recorder.prompt)

if __name__ == '__main__': unittest.main()
