import unittest

from .post_lesson import memory_payload, records


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

    def test_reused_transcript_id_does_not_overwrite_a_prior_paragraph(self):
        snapshot=self.snapshot()
        for event in snapshot['events']:
            event['payload']['projection_version']=1
        snapshot['events'][1]['created']='2026-09-27T00:01:10Z'
        utterances=[item for item in records(snapshot) if item['kind']=='live_transcript']
        self.assertEqual([item['data']['text'] for item in utterances],['我不','會算'])
        self.assertEqual([item['data']['sources'] for item in utterances],[['e1'],['e2']])
        snapshot['events'][1]['created']=snapshot['events'][0]['created']
        snapshot['events'][0]['payload']['text']='甲'*8000
        utterances=[item for item in records(snapshot) if item['kind']=='live_transcript']
        self.assertEqual(''.join(item['data']['text'] for item in utterances),'甲'*8000+'會算')

    def test_speakers_are_not_merged_into_one_student_utterance(self):
        snapshot=self.snapshot()
        snapshot['events'][0]['payload']['speaker']='student'
        snapshot['events'][1]['payload']['speaker']='parent'
        utterances=[item for item in records(snapshot) if item['kind']=='live_transcript']
        self.assertEqual([item['data']['speaker'] for item in utterances],['student','parent'])
        self.assertEqual([item['data']['text'] for item in utterances],['我不','會算'])

    def test_memory_payload_orders_questions_chronologically_and_excludes_parent(self):
        snapshot={
            'session':{'id':'session-2','started':'2026-09-27T00:00:00Z',
                       'grade':2,'mode':'basic','notes':'練習加法'},
            'events':[
                {'id':'e1','kind':'student_utterance','created':'2026-09-27T00:01:00Z',
                 'payload':{'text':'第一題不會'}},
                {'id':'e2','kind':'live_transcript','created':'2026-09-27T00:02:00Z',
                 'payload':{'transcript_id':'t1','role':'user','speaker':'parent','text':'家長問句'}},
                {'id':'e3','kind':'live_transcript','created':'2026-09-27T00:03:00Z',
                 'payload':{'transcript_id':'t2','role':'user','speaker':'student','text':'第二題怎麼算'}},
                {'id':'e4','kind':'student_utterance','created':'2026-09-27T00:04:00Z',
                 'payload':{'text':'第三題懂了'}},
                {'id':'e5','kind':'student_utterance','created':'2026-09-27T00:05:00Z',
                 'payload':{'text':'家長打字','speaker':'parent'}},
                {'id':'e6','kind':'live_transcript','created':'2026-09-27T00:06:00Z',
                 'payload':{'role':'user','speaker':'onsite_teacher','text':'現場老師說話'}},
            ],
            'evidence':[],
        }
        payload=memory_payload(snapshot)
        self.assertEqual(payload['student_questions'],['第一題不會','第二題怎麼算','第三題懂了'])
