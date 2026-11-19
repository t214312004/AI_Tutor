import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from .agy_runtime import AgyLiveRuntime
from .agy_voice import AgyAudioEvent, MIC_FRAME_BYTES
from .tutor import TutorSession


class TestAgyRuntime(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.events, self.records, self.peers = [], [], []
        self.observer = type('Observer', (), {'workspace': root, 'vision_only': True,
            'turn': AsyncMock(return_value='相片上的唯一代號 A19'), 'close': AsyncMock()})()

        async def emit(event):
            self.events.append(event)

        def connect(generation, rules, hook):
            class Peer:
                conversation = '72482e65-c35f-4baa-b8dd-a90b6a3fd55e'

                def __init__(self):
                    self.proposal = root / f'{generation}.json'
                    self.proposal.write_text('{}')
                    self.media = asyncio.Queue()
                    self.bridge = self
                    self.sent = []
                    self.closed = False
                    self.write = AsyncMock()

                async def start(self):
                    pass

                async def close(self):
                    self.closed = True

                async def receive(self):
                    return await self.media.get()

                def feed_microphone(self, data, *, generation):
                    self.sent.append((data, generation))

                def clear_microphone(self, *, generation):
                    self.sent.clear()

            peer = Peer()
            self.peers.append(peer)
            return peer

        session = TutorSession('synthetic', 2, basic_workflow=True, input_speaker='student')
        self.runtime = AgyLiveRuntime(session, self.observer, emit,
            lambda kind, data: self.records.append((kind, data)), connection_factory=connect)
        self.addAsyncCleanup(self.runtime.close)
        await self.runtime.connect_voice('first')
        await asyncio.sleep(0)

    async def test_old_media_is_revoked_and_restart_keeps_mic_setting(self):
        self.runtime.set_mic(True)
        self.runtime.accept_media(bytes(MIC_FRAME_BYTES), 'first')
        self.runtime.accept_media(bytes(MIC_FRAME_BYTES), 'wrong')
        self.assertEqual(len(self.peers[0].sent), 1)
        self.peers[0].media.put_nowait(AgyAudioEvent('audio', 1, b'\x01\x00'))
        await asyncio.sleep(0)
        self.assertTrue(any(e['type'] == 'agy_pcm' and e['rtc_id'] == 'first' for e in self.events))
        self.runtime.set_mic(False)
        await self.runtime.interrupt()
        self.assertTrue(self.peers[0].closed)
        await self.runtime.connect_voice('second')
        await asyncio.sleep(0)
        self.runtime.accept_media(bytes(MIC_FRAME_BYTES), 'first')
        self.runtime.accept_media(bytes(MIC_FRAME_BYTES), 'second')
        self.assertEqual(self.peers[1].sent, [])
        self.assertFalse(self.runtime.mic_enabled)

    async def test_photo_uses_hook_and_board_waits_for_user_confirmation(self):
        peer = self.peers[0]
        self.runtime.accept_frame('data:image/jpeg;base64,/9j/c3ludGhldGlj')
        await self.runtime.observations.task
        await self.runtime.photo_update_task
        self.assertFalse(any(e['type'] == 'agy_pcm' for e in self.events))
        peer.write.assert_not_called()
        photo = self.runtime.context()['photo']
        self.assertIn(photo['ref'], await self.runtime.handle_hook(peer, 0))
        self.assertNotIn('A19', await self.runtime.handle_hook(peer, 0))
        peer.proposal.write_text(json.dumps({'tool': 'get_photo_observation', 'ref': photo['ref']}))
        self.assertIn('A19', await self.runtime.handle_hook(peer, 1))
        peer.proposal.write_text(json.dumps({'tool': 'update_whiteboard', 'photo_ref': '', 'lines': ['2 + 3 = 5']}))
        self.assertIn('請提供本次核對的照片索引', await self.runtime.handle_hook(peer, 2))
        self.assertIsNone(self.runtime.pending_tool)
        peer.proposal.write_text(json.dumps({'tool': 'update_whiteboard', 'photo_ref': photo['ref'], 'lines': ['2 + 3 = 5']}))
        result = await self.runtime.handle_hook(peer, 3)
        self.assertIn('等待使用者', result)
        self.assertIn('A19', result)
        self.assertFalse(any(e['type'] == 'board' for e in self.events))
        proposal = next(e['id'] for e in self.events if e['type'] == 'agy_classroom_proposal')
        await self.runtime.confirm_tool(proposal, True)
        await self.runtime.confirm_tool(proposal, True)
        self.assertEqual(sum(e['type'] == 'board' for e in self.events), 1)
        self.assertIn('已由使用者確認', await self.runtime.handle_hook(peer, 4))
        self.assertNotIn('A19', await self.runtime.handle_hook(peer, 0))

    async def test_withdrawn_photo_does_not_reappear_in_hook_result(self):
        peer = self.peers[0]
        self.runtime.accept_frame('data:image/jpeg;base64,/9j/c3ludGhldGlj')
        await self.runtime.observations.task
        photo = self.runtime.context()['photo']
        peer.proposal.write_text(json.dumps({'tool': 'get_photo_observation', 'ref': photo['ref']}))
        self.assertIn('A19', await self.runtime.handle_hook(peer, 1))
        self.runtime.set_camera(False)
        peer.proposal.write_text(json.dumps({'tool': 'update_whiteboard', 'photo_ref': photo['ref'], 'lines': ['2 + 3 = 5']}))
        self.assertNotIn('A19', await self.runtime.handle_hook(peer, 2))
        self.assertIsNone(self.runtime.pending_tool)

    async def test_homework_edit_uses_confirmation_and_no_inferred_completion(self):
        peer = self.peers[0]
        await self.runtime.handle_hook(peer, 0)
        peer.proposal.write_text(json.dumps({'tool': 'update_homework_plan',
            'tasks': [{'id': 'math', 'title': '數學習作第 10 頁'}]}), encoding='utf-8')
        await self.runtime.handle_hook(peer, 1)
        self.assertEqual(self.runtime.session.tasks, [])
        proposal = next(e for e in self.events if e['type'] == 'agy_classroom_proposal')
        await self.runtime.confirm_tool(proposal['id'], True)
        self.assertFalse(self.runtime.session.plan_confirmed)
        self.assertEqual(self.runtime.session.tasks[0]['status'], 'todo')
