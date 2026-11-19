"""One isolated Windows terminal, hook channel and PCM connection per Live generation."""

import asyncio
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading

from .agy_profile import AgyPrivateProfile, agy_executable, agy_onboarding
from .agy_voice import AgyAudioBridge
from .providers import ProviderError


_VT = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')
_CONVERSATION = re.compile(r'[0-9a-f-]{36}\Z', re.IGNORECASE)


class AgyLiveConnection:
    def __init__(self, generation, instructions, on_hook):
        self.generation = generation
        self.instructions = instructions
        self.on_hook = on_hook
        self.profile = self.bridge = self.server = self.terminal = self.reader = None
        self.output = ''
        self.conversation = None
        self.token = secrets.token_urlsafe(32)
        self.closed = False
        self.cleaned = False
        self.started_voice = False
        self.early_events = []
        self._close_lock = asyncio.Lock()

    async def start(self):
        onboarding = await asyncio.to_thread(agy_onboarding)
        try:
            from winpty import PtyProcess
        except ImportError as error:
            raise ProviderError('請先執行專案安裝以啟用 agy Live。') from error
        prepare = asyncio.create_task(asyncio.to_thread(AgyPrivateProfile))
        try:
            self.profile = profile = await asyncio.shield(prepare)
        except asyncio.CancelledError:
            self.profile = await prepare
            raise
        self.bridge = await AgyAudioBridge.open(self.generation)
        self.server = await asyncio.start_server(self.handle_hook, '127.0.0.1', 0, limit=2048)
        _, port = self.server.sockets[0].getsockname()
        def write_files():
            config = profile.root / 'hook-channel.json'
            config.write_text(json.dumps({'token': self.token, 'port': port}), encoding='utf-8')
            command = subprocess.list2cmdline([sys.executable, '-I', str(Path(__file__).with_name('agy_hook.py')), str(config)])
            hooks = profile.workspace / '.agents'
            hooks.mkdir()
            (hooks / 'hooks.json').write_text(json.dumps({'classroom': {
                'PreInvocation': [{'command': command, 'timeout': 4}],
            }}), encoding='utf-8')
            (profile.workspace / 'AGENTS.md').write_text(self.instructions, encoding='utf-8')
            self.proposal.write_text('{}', encoding='utf-8')
            cache = profile.app_data / 'cache'
            cache.mkdir(parents=True)
            (cache / 'onboarding.json').write_text(json.dumps(onboarding), encoding='utf-8')
            settings = {
                'modelProvider': '', 'colorScheme': 'terminal', 'altScreenMode': 'never',
                'toolPermission': 'request-review', 'trustedWorkspaces': [str(profile.workspace)],
                'permissions': {
                    'allow': [f'write_file({self.proposal.as_posix()})'],
                    'deny': ['command(*)', 'read_url(*)', 'execute_url(*)', 'mcp(*)',
                             f'write_file({hooks.as_posix()}/*)', f'write_file({(profile.workspace / "AGENTS.md").as_posix()})',
                             f'read_file({config.as_posix()})'],
                },
            }
            settings_path = profile.root / 'settings.json'
            settings_path.write_text(json.dumps(settings), encoding='utf-8')
            return settings_path
        settings_path = await asyncio.to_thread(write_files)
        launch = asyncio.create_task(asyncio.to_thread(PtyProcess.spawn, [
            str(agy_executable()), '--sandbox', '--settings', str(settings_path), '--log-file', os.devnull,
        ], cwd=str(profile.workspace), env={**profile.env, 'ANTIGRAVITY_MIC': self.bridge.address}, dimensions=(38, 150)))
        try:
            self.terminal = await asyncio.shield(launch)
        except asyncio.CancelledError:
            self.terminal = await launch
            raise
        self.reader = threading.Thread(target=self.read_terminal, daemon=True)
        self.reader.start()
        async with asyncio.timeout(25):
            while self.terminal.isalive() and '? for shortcuts' not in self.output:
                if 'Terms of Service & Data Use' in self.output:
                    raise ProviderError('agy 仍需完成首次設定，請在專用設定視窗完成選擇。')
                await asyncio.sleep(.1)
            if not self.terminal.isalive():
                raise ProviderError('agy 未能啟動。請檢查 CLI 安裝及訂閱登入。')
            # The shortcut footer can be rendered before the input composer accepts a command.
            await asyncio.sleep(2)
            await self.write('/voice live')
            media_ready = voice_selected = False
            while True:
                if media_ready and 'Started voice chat' in self.output:
                    self.started_voice = True
                    return
                if not voice_selected and re.search(r'Choose Antigravity[’\']s Voice', self.output, re.IGNORECASE):
                    voice_selected = True
                    await self.write('')
                if not self.terminal.isalive():
                    raise ProviderError('agy 語音啟動前已關閉。')
                try:
                    event = await asyncio.wait_for(self.bridge.receive(), .2)
                except TimeoutError:
                    continue
                if event.kind == 'ready':
                    media_ready = True
                elif event.kind in ('audio', 'clear'):
                    if len(self.early_events) < 32:
                        self.early_events.append(event)
                elif event.kind in ('closed', 'disconnected'):
                    raise ProviderError('agy 語音音訊連線中斷。')

    @property
    def proposal(self):
        return self.profile.workspace / 'classroom-request.json'

    def read_terminal(self):
        try:
            while self.terminal.isalive() and not self.closed:
                chunk = self.terminal.read(65536)
                if not chunk:
                    break
                if not self.started_voice:
                    self.output = (self.output + _VT.sub('', chunk))[-16_000:]
        except (OSError, EOFError):
            pass

    async def write(self, text):
        await asyncio.to_thread(self.terminal.write, text + '\r')

    async def handle_hook(self, reader, writer):
        try:
            async with asyncio.timeout(2):
                payload = json.loads(await reader.readline())
                token, conversation, invocation = (payload.get(k) for k in ('token', 'conversation', 'invocation'))
                if (not self.closed and isinstance(token, str) and hmac.compare_digest(self.token, token)
                        and isinstance(conversation, str) and _CONVERSATION.fullmatch(conversation)
                        and type(invocation) is int and 0 <= invocation < 100_000
                        and self.conversation in (None, conversation)):
                    self.conversation = conversation
                    notice = await self.on_hook(self, invocation)
                    writer.write(json.dumps(notice, ensure_ascii=False).encode() + b'\n')
                    await writer.drain()
        except (OSError, ValueError, AttributeError, TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (OSError, TimeoutError):
                pass

    async def close(self):
        async with self._close_lock:
            if self.cleaned:
                return
            self.closed = True
            try:
                if self.bridge:
                    await self.bridge.close()
                if self.server:
                    self.server.close()
                    await self.server.wait_closed()
                terminal = self.terminal
                if terminal and terminal.isalive():
                    try:
                        await asyncio.to_thread(subprocess.run, [
                            str(Path(os.environ['SystemRoot']) / 'System32' / 'taskkill.exe'),
                            '/PID', str(terminal.pid), '/T', '/F',
                        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW, timeout=8)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    try:
                        await asyncio.to_thread(terminal.close)
                    except Exception:
                        pass
                    deadline = asyncio.get_running_loop().time() + 2
                    while terminal.isalive() and asyncio.get_running_loop().time() < deadline:
                        await asyncio.sleep(.05)
                    if terminal.isalive():
                        raise ProviderError('agy 舊程序仍在關閉中，請結束本次陪讀。')
                elif terminal:
                    try:
                        await asyncio.to_thread(terminal.close)
                    except Exception:
                        pass
            finally:
                if self.reader:
                    await asyncio.to_thread(self.reader.join, 2)
            if self.profile:
                await asyncio.to_thread(self.profile.close)
            self.cleaned = True
