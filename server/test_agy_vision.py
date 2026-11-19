import asyncio
import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from .agy_vision import AgyAcpVision, MAX_RESPONSE_CHARS
from .providers import ProviderError


PNG = b'\x89PNG\r\n\x1a\nsynthetic'
JPEG = b'\xff\xd8\xffsynthetic'


def image_url(mime, data):
    return f'data:{mime};base64,' + base64.b64encode(data).decode('ascii')


class FakeAcp:
    def __init__(self, scenario):
        self.scenario = scenario
        self.stdin = self
        self.stdout = asyncio.StreamReader()
        self.messages = []
        self.prompt_started = asyncio.Event()
        self.finished = asyncio.Event()
        self.returncode = None

    def emit(self, **value):
        self.stdout.feed_data((json.dumps({'jsonrpc': '2.0', **value}) + '\n').encode())

    def text(self, session_id, text, kind='agent_message_chunk'):
        self.emit(method='session/update', params={'sessionId': session_id, 'update': {
            'sessionUpdate': kind, 'content': {'type': 'text', 'text': text},
        }})

    def write(self, payload):
        message = json.loads(payload)
        self.messages.append(message)
        method = message.get('method')
        ident = message.get('id')
        if method == 'initialize':
            self.emit(id=ident, result={'protocolVersion': 1, 'agentCapabilities': {'promptCapabilities': {'image': True}}})
        elif method == 'session/new':
            self.emit(id=ident, result={'sessionId': 'current'})
        elif method == 'session/prompt':
            self.prompt_started.set()
            if self.scenario == 'stalled':
                return
            if self.scenario == 'permission':
                self.emit(id=901, method='session/request_permission', params={'sessionId': 'current'})
                return
            if self.scenario == 'oversize':
                self.text('current', 'x' * (MAX_RESPONSE_CHARS + 1))
                return
            self.emit(id=ident + 100, result={'stopReason': 'end_turn'})
            self.text('old', '舊照片不應混入')
            self.text('current', '輸入回音不應混入', 'user_message_chunk')
            self.text('current', '辨識：')
            self.text('current', '37 + 28')
            self.emit(id=ident, result={'stopReason': 'end_turn'})

    async def drain(self):
        pass

    def close(self):
        self.returncode = 0
        self.stdout.feed_eof()
        self.finished.set()

    async def wait(self):
        await self.finished.wait()


class AgyVisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        profile = SimpleNamespace(workspace=root / 'empty-workspace', gemini=root / '.gemini',
                                  app_data=root / '.gemini' / 'antigravity-cli', env={'OS': 'synthetic'},
                                  close=self.folder.cleanup)
        profile.workspace.mkdir()
        with patch('server.agy_vision.AgyPrivateProfile', return_value=profile):
            self.vision = AgyAcpVision()
        self.addAsyncCleanup(self.vision.close)
        self.processes = []
        self.scenario = 'success'

        async def spawn(*args, **kwargs):
            process = FakeAcp(self.scenario)
            self.processes.append(process)
            return process

        self.spawn = AsyncMock(side_effect=spawn)
        self.enterContext(patch('server.agy_vision.agy_executable', return_value=root / 'agy.exe'))
        self.enterContext(patch('server.agy_vision.asyncio.create_subprocess_exec', self.spawn))

    async def test_ready_is_local_and_each_turn_uses_one_image_in_a_new_session(self):
        await self.vision.ensure_ready()
        self.assertEqual([m['method'] for m in self.processes[0].messages], ['initialize', 'session/new'])

        for mime, data in (('image/png', PNG), ('image/jpeg', JPEG)):
            self.assertEqual(await self.vision.turn('請辨識題目', image_url(mime, data)), '辨識：37 + 28')
            process = self.processes[-1]
            self.assertEqual([m['method'] for m in process.messages], ['initialize', 'session/new', 'session/prompt'])
            self.assertEqual(process.messages[1]['params']['mcpServers'], [])
            prompt = process.messages[2]['params']
            self.assertEqual(prompt['sessionId'], 'current')
            self.assertEqual(prompt['prompt'][1], {'type': 'image', 'mimeType': mime, 'data': base64.b64encode(data).decode()})
            self.assertEqual(process.returncode, 0)

        args, options = self.spawn.call_args
        self.assertIn('--include-default-customizations=false', args)
        self.assertIn('--sandbox', args)
        self.assertEqual(options['env'], self.vision.profile.env)
        self.assertEqual(list(options['cwd'].iterdir()), [])
        settings = json.loads((self.vision.profile.app_data / 'settings.json').read_text(encoding='utf-8'))
        self.assertEqual(set(settings['permissions']['deny']), {
            'read_file(*)', 'write_file(*)', 'read_url(*)', 'execute_url(*)', 'command(*)', 'mcp(*)',
        })

    async def test_invalid_image_never_launches_a_process(self):
        for url in (None, 'https://invalid.test/a.png', 'data:image/png;base64,@@@', image_url('image/jpeg', PNG)):
            with self.subTest(url=url), self.assertRaises(ValueError):
                await self.vision.turn('請辨識題目', url)
        self.spawn.assert_not_awaited()

    async def test_server_tool_permission_is_rejected_and_observation_fails(self):
        self.scenario = 'permission'
        with self.assertRaisesRegex(ProviderError, '未授權的工具'):
            await self.vision.turn('請辨識題目', image_url('image/png', PNG))
        self.assertEqual(self.processes[0].messages[-1], {
            'jsonrpc': '2.0', 'id': 901, 'result': {'outcome': {'outcome': 'cancelled'}},
        })
        self.assertEqual(self.processes[0].returncode, 0)

    async def test_observation_length_is_limited(self):
        self.scenario = 'oversize'
        with self.assertRaisesRegex(ProviderError, '結果過大'):
            await self.vision.turn('請辨識題目', image_url('image/png', PNG))
        self.assertEqual(self.processes[0].returncode, 0)

    async def test_timeout_cancels_exact_session_and_reaps_process(self):
        self.scenario = 'stalled'
        with self.assertRaises(TimeoutError):
            await self.vision.turn('請辨識題目', image_url('image/png', PNG), timeout=.1)
        self.assertEqual(self.processes[0].messages[-1], {
            'jsonrpc': '2.0', 'method': 'session/cancel', 'params': {'sessionId': 'current'},
        })
        self.assertEqual(self.processes[0].returncode, 0)

    async def test_close_cancels_active_turn_and_cleans_private_files(self):
        self.scenario = 'stalled'
        turn = asyncio.create_task(self.vision.turn('請辨識題目', image_url('image/png', PNG)))
        while not self.processes:
            await asyncio.sleep(0)
        await asyncio.wait_for(self.processes[0].prompt_started.wait(), 1)
        await self.vision.close()
        with self.assertRaises(asyncio.CancelledError):
            await turn
        self.assertEqual(self.processes[0].returncode, 0)
        self.assertFalse(self.vision.workspace.exists())
        with self.assertRaisesRegex(ProviderError, '已關閉'):
            await self.vision.ensure_ready()
