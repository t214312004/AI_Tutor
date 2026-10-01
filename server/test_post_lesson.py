import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .post_lesson import SECTIONS,analyze,records,validate
from .audit import SessionAudit


class PostLessonTests(unittest.IsolatedAsyncioTestCase):
    def snapshot(self):
        return {'session':{'id':'session-1','started':'2026-09-27T00:00:00Z',
                           'grade':2,'mode':'gpt','notes':'練習加法'},
                'events':[{'id':'e1','kind':'live_transcript','created':'2026-09-27T00:01:00Z',
                           'payload':{'transcript_id':'t1','role':'user','text':'我不'}},
                          {'id':'e2','kind':'live_transcript','created':'2026-09-27T00:01:01Z',
                           'payload':{'transcript_id':'t1','role':'user','text':'會算'}}],
                'evidence':[]}

    async def test_live_transcript_parts_become_one_sourced_turn(self):
        entries=records(self.snapshot())
        utterance=next(item for item in entries if item['kind']=='live_transcript')
        self.assertEqual(utterance['id'],'e1')
        self.assertEqual(utterance['data']['text'],'我不會算')
        self.assertEqual(len([item for item in entries if item['kind']=='live_transcript']),1)

    async def test_ai_summary_requires_real_source_ids(self):
        answer={key:[] for key in SECTIONS}
        answer['difficulties']=[{'text':'學生詢問加法','sources':['e1']}]
        class FakeProvider:
            def __init__(self,workspace):pass
            async def turn(self,prompt,**kwargs):return json.dumps(answer,ensure_ascii=False)
            async def close(self):pass
        with tempfile.TemporaryDirectory() as folder,patch('server.post_lesson.LazyCodex',FakeProvider):
            root=Path(folder)
            audit=SessionAudit(root/'audit','session-1','gpt','codex',root/'teacher')
            result,provider,turns=await analyze(self.snapshot(),'codex',root,audit=audit)
            entries=[json.loads(line) for line in audit.events.read_text(encoding='utf-8').splitlines()]
            self.assertTrue(any(item['kind']=='agent_request' and
                                '課後學習資料整理員' in item['prompt'] for item in entries))
            self.assertTrue(any(item['kind']=='agent_response' for item in entries))
        self.assertEqual(result['difficulties'][0]['sources'],['e1'])
        self.assertEqual((provider,turns),('codex',1))
        answer['difficulties'][0]['sources']=['not-in-this-session']
        with self.assertRaisesRegex(ValueError,'不存在的來源'):
            validate(json.dumps(answer,ensure_ascii=False),{'e1'})

    async def test_configured_responses_summary_uses_selected_model(self):
        snapshot=self.snapshot()
        snapshot['events'].append({'id':'cfg','kind':'session_config','created':'2026-09-27T00:00:00Z',
            'payload':{'gpt_live':{'version':1,
                'delegation':{'type':'responses','model':'gpt-6.1-sol','effort':'low'},
                'vision':{'provider':'openai','model':'gpt-6-luna','effort':'max'}}}})
        chosen=[];usage=[]
        class FakeResponses:
            def __init__(self,key,model,effort):
                chosen.append((key,model,effort));self.usage=[{'input_tokens':4}]
            async def turn(self,prompt,**kwargs):
                return json.dumps({key:[] for key in SECTIONS})
            async def close(self):pass
        with tempfile.TemporaryDirectory() as folder,\
             patch('server.openai_vision.OpenAIResponsesProvider',FakeResponses):
            _,provider,_=await analyze(snapshot,'gpt_configured',Path(folder),
                                       openai_key='test-key',usage_callback=usage.append)
        self.assertEqual(chosen,[('test-key','gpt-6.1-sol','low')])
        self.assertEqual(provider,'openai')
        self.assertEqual(usage,[{'input_tokens':4}])
