"""Opt-in, paid OpenAI integration checks using synthetic inputs only.

Reads the application's DPAPI store in memory; never exports credentials.
Does not open real devices or write to the household student database.
"""
import argparse
import asyncio
import base64
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import aiohttp
import httpx
from server.gpt_config import verify_openai_models
from server.live import GPTLive
from server.live_runtime import LiveRuntime
from server.openai_vision import OpenAIResponseVision
from server.post_lesson import analyze
from server.tutor import TutorEngine, TutorSession

CONFIG = {'version': 1,
          'delegation': {'type': 'responses', 'model': 'gpt-6.1-sol', 'effort': 'low'},
          'vision': {'provider': 'openai', 'model': 'gpt-6-luna', 'effort': 'max'}}


def read_key():
    result = subprocess.run(['node', '-e',
        'process.stdout.write(require("./electron/shared-storage.cjs").readKeys().OPENAI_API_KEY || "")'],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError('OpenAI key unavailable in application credential store')
    return result.stdout.strip()


def error_info(error, key):
    info = {'type': type(error).__name__, 'message': str(error).replace(key, '[REDACTED]')[:500]}
    cause = error
    while cause:
        if isinstance(cause, httpx.HTTPStatusError):
            info['http'] = cause.response.status_code
            try:
                detail = cause.response.json().get('error', {})
                info['api_error'] = {k: str(detail.get(k, '')).replace(key, '[REDACTED]')[:500]
                                     for k in ('code', 'type', 'message', 'param')}
            except ValueError:
                pass
        if isinstance(cause, aiohttp.WSServerHandshakeError):
            info['http'] = cause.status
        cause = cause.__cause__
    return info


async def vision(key, output):
    image = 'data:image/png;base64,' + base64.b64encode(
        (ROOT / 'test-results/synthetic-worksheet.png').read_bytes()).decode()
    provider = OpenAIResponseVision(key, CONFIG['vision']['model'], CONFIG['vision']['effort'])
    try:
        session = TutorSession('synthetic-openai-probe', 2, student_name='合成測試', teacher_name='測試老師')
        result = await TutorEngine(provider).respond(session, '只記錄照片可見事實。',
                                                    image_url=image, observation=True)
        output.update(result=result, usage=provider.usage)
        assert result is not None, 'Observation discarded: exceeded the runtime 30-second freshness limit'
        evidence = ' '.join(result['evidence'])
        assert all(number in evidence for number in ('7', '5', '12')), 'Visible equation not identified'
        assert result['quiet'] and not result['speech'] and not result['board_html']
        assert result['lesson']['signal'] == 'none', 'A single fixed photo cannot prove new progress'
    finally:
        await provider.close()


async def summary(key, output):
    snapshot = {'session': {'id': 'synthetic-session', 'started': '2026-09-30T00:00:00Z',
                           'grade': 2, 'mode': 'gpt', 'notes': '人工合成紀錄'},
                'events': [
                    {'id': 'config', 'kind': 'session_config', 'created': '2026-09-30T00:00:00Z',
                     'payload': {'gpt_live': CONFIG}},
                    {'id': 'question', 'kind': 'student_utterance', 'created': '2026-09-30T00:00:01Z',
                     'payload': {'text': '七加五怎麼算？'}},
                    {'id': 'hint', 'kind': 'text_reply', 'created': '2026-09-30T00:00:02Z',
                     'payload': {'text': '先想七還差多少到十。'}},
                    {'id': 'claimed', 'kind': 'student_utterance', 'created': '2026-09-30T00:00:03Z',
                     'payload': {'text': '我寫完了。'}}], 'evidence': []}
    usage = []
    result, provider, turns = await analyze(snapshot, 'gpt_configured', ROOT / '.local/openai-probe',
                                          openai_key=key, usage_callback=usage.append)
    output.update(result=result, provider=provider, turns=turns, usage=usage)
    assert provider == 'openai' and turns == 1 and usage
    assert result['uncertainties'], 'Missing uncertainty for unobserved work and unverified playback'


async def camera(key, output):
    path = Path(output.pop('image_path'))
    pixels = path.read_bytes()
    mime = 'png' if pixels.startswith(b'\x89PNG\r\n\x1a\n') else 'jpeg'
    image = 'data:image/' + mime + ';base64,' + base64.b64encode(pixels).decode()
    provider = OpenAIResponseVision(key, CONFIG['vision']['model'], CONFIG['vision']['effort'])
    try:
        session = TutorSession('authorized-camera-probe', 2, teacher_name='測試老師')
        result = await TutorEngine(provider).respond(session,
            '這是使用者授權的真實國語作業測試。辨識你能可靠讀出的題型、題目與手寫作答；'
            '模糊字或猜測請明確標示。不記錄姓名，不把單張圖片推論為學生已熟悉或有新進步。',
            image_url=image, observation=True)
        output.update(result=result, usage=provider.usage, source='user-authorized real camera')
        assert result is not None, 'Real photo observation exceeded runtime 30-second freshness limit'
        assert result['quiet'] and not result['speech']
    finally:
        await provider.close()


async def client_audio(key, output):
    """Exercise real Live PCM, transcripts, client delegation and final usage."""
    audio_path = ROOT / 'test-results/openai-probe-input.wav'
    synthesis = '''Add-Type -AssemblyName System.Speech
$taskVoice = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  $taskVoice.SelectVoice('Microsoft Hanhan Desktop')
  $taskFormat = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(24000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
  $taskVoice.SetOutputToWaveFile($args[0], $taskFormat)
  $taskVoice.Speak('老師，七加五怎麼算？請給我一個提示。')
} finally { $taskVoice.Dispose() }
'''
    # The temporary script contains only the fixed synthetic sentence.
    synth_path = ROOT / 'test-results/openai-probe-synthesis.ps1'
    synth_path.write_text(synthesis, encoding='utf-8-sig')
    completed = await asyncio.to_thread(subprocess.run,
        ['powershell.exe', '-NoProfile', '-File', str(synth_path), str(audio_path)],
        capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    assert completed.returncode == 0, 'Synthetic SAPI audio generation failed'
    with wave.open(str(audio_path), 'rb') as source:
        assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (1, 2, 24000)
        pcm = source.readframes(source.getnframes())
    output['input_audio_seconds'] = len(pcm) / 48000
    provider = GPTLive(key, teacher_name='測試老師')
    counts = Counter()
    input_text, spoken_text, audio = [], [], bytearray()
    started, answered, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    errors = []
    last_audio = 0

    async def read():
        nonlocal last_audio
        async for event in provider.events():
            kind = event.get('type', '')
            counts[kind] += 1
            if kind == 'session.started': started.set()
            elif kind == 'session.input_transcript.delta': input_text.append(event.get('delta', ''))
            elif kind == 'session.output_transcript.delta': spoken_text.append(event.get('delta', ''))
            elif kind == 'session.output_audio.delta':
                audio.extend(base64.b64decode(event['delta']))
                last_audio = time.monotonic()
            elif kind == 'session.delegation.created':
                delegation = event.get('delegation', {})
                await provider.append('可以先想七還差多少到十，再把五拆開。', delegation.get('id'), speak=True)
            elif kind == 'session.output_audio.done': answered.set()
            elif kind == 'session.closed': closed.set(); break
            elif kind == 'error': errors.append(event.get('error', {})); started.set(); answered.set()

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=12)) as http:
        reader = None
        try:
            await provider.connect(http)
            reader = asyncio.create_task(read())
            await asyncio.wait_for(started.wait(), 18)
            assert provider.ready and not errors, 'Live session failed to start'
            await provider.mute(True)
            await provider.mute(False)
            # Pace synthetic PCM like a microphone; also supply trailing silence for turn detection.
            stream = pcm + bytes(48000 * 2)
            for offset in range(0, len(stream), 4800):
                await provider.send_audio(stream[offset:offset + 4800])
                await asyncio.sleep(0.1)
            # GPT-Live does not promise a Realtime-style output_audio.done event.
            # Continue clocked silence and observe a pause in returned speech.
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                await provider.send_audio(bytes(4800))
                await asyncio.sleep(.1)
                if audio and spoken_text and time.monotonic() - last_audio > 1.5:
                    break
            assert audio and input_text and spoken_text and not errors, 'Missing audio or transcripts'
            assert any(s in ''.join(input_text) for s in ('七', '7')) and any(s in ''.join(input_text) for s in ('五', '5'))
        finally:
            if provider.ready:
                await provider.request_close()
                try: await asyncio.wait_for(closed.wait(), 15)
                except TimeoutError: pass
            await provider.release()
            if reader:
                reader.cancel(); await asyncio.gather(reader, return_exceptions=True)
            output.update(events=dict(counts), input_transcript=''.join(input_text),
                          output_transcript=''.join(spoken_text), audio_bytes=len(audio),
                          errors=errors, usage=provider.ledger.report(), ledger=provider.ledger.gpt)
            if audio:
                with wave.open(str(ROOT / 'test-results/openai-probe-output.wav'), 'wb') as target:
                    target.setnchannels(1); target.setsampwidth(2); target.setframerate(24000)
                    target.writeframes(audio)
    assert provider.ledger.report()['gpt_final'], 'Final usage not received'


async def live_tools(key, output):
    """Use the actual runtime, independent vision and Responses function round trips."""
    photo_test = output['stage'] == 'live-photo'
    provider = OpenAIResponseVision(key, CONFIG['vision']['model'], CONFIG['vision']['effort'])
    session = TutorSession('synthetic-openai-probe', 2, student_name='合成測試', teacher_name='測試老師')
    events, saved, wire_counts, api_errors = [], [], Counter(), []
    done = asyncio.Event()
    last_pcm = 0
    async def emit(event):
        nonlocal last_pcm
        events.append({k: v for k, v in event.items() if k != 'audio'})
        if event['type'] == 'pcm':
            last_pcm = time.monotonic()
            done.set()
    def persist(kind, payload): saved.append({'kind': kind, 'payload': payload})
    runtime = LiveRuntime(session, provider, provider, 'gpt', key, emit, persist, gpt_config=CONFIG)
    original = runtime.provider.normalize
    def normalize(event):
        wire_counts[event.get('type')] += 1
        if event.get('type') == 'error': api_errors.append(event.get('error', {}))
        if event.get('type') == 'response.event':
            wire_counts['nested:' + event.get('event', {}).get('type', '')] += 1
        return original(event)
    runtime.provider.normalize = normalize
    tool_names = []
    original_tool = runtime.responses_tool
    async def tool(name, args, ticket=None):
        tool_names.append(name)
        return await original_tool(name, args, ticket)
    runtime.responses_tool = tool
    image = 'data:image/jpeg;base64,' + base64.b64encode(
        (ROOT / 'test-results/synthetic-worksheet.jpg').read_bytes()).decode()
    clock_task = None
    try:
        await runtime.start()
        async def clock_audio():
            while not runtime.closing:
                runtime.accept_audio(bytes(4800))
                await asyncio.sleep(.1)
        clock_task = asyncio.create_task(clock_audio())
        # Set a known synthetic frame, then ask the actual runtime tool to inspect it.
        runtime.frame = {'id': 'synthetic-photo', 'image': image, 'received': time.monotonic(),
                         'camera_epoch': runtime.camera_epoch, 'captured_at': datetime.now(timezone.utc).isoformat()}
        request = ('這是程式整合測試。請先呼叫 get_lesson_context，'
                   '再呼叫 get_curriculum_references 查詢小二加法，'
                   '最後用 set_board 顯示七加五的湊十法提示，並口頭說明一個提示。')
        if photo_test:
            request = ('這是圖片工具整合測試。請先呼叫 inspect_photo，fresh=false，'
                       '只根據它回傳的照片證據說明照片中的七加五算式，'
                       '再用 set_board 顯示照片中看得到的算式，口頭說明照片內容。')
        runtime.accept_text(request)
        deadline = time.monotonic() + 55
        while time.monotonic() < deadline:
            spoken = runtime.transcripts['assistant']
            if (any(event['type'] == 'board' for event in events) and done.is_set() and not runtime.tools
                    and time.monotonic() - last_pcm > 1.5
                    and any(x in spoken for x in ('七', '7')) and any(x in spoken for x in ('五', '5'))):
                break
            if runtime.failure or api_errors: break
            await asyncio.sleep(0.2)
        assert not runtime.failure and not api_errors, 'Real Live event processing failed'
        assert any(event['type'] == 'board' for event in events), 'No board function reached the runtime'
        assert done.is_set(), 'No spoken PCM returned'
        assert runtime.response_usage, 'No completed backend Responses usage'
        spoken = runtime.transcripts['assistant']
        assert any(x in spoken for x in ('七', '7')) and any(x in spoken for x in ('五', '5')), 'Live speech did not reflect the requested arithmetic'
        if photo_test:
            assert 'inspect_photo' in tool_names and provider.usage, 'Independent vision route was not invoked'
            assert session.observation and session.observation['capture_id'] == 'synthetic-photo', 'No current photo observation reached the runtime'
    finally:
        if clock_task:
            clock_task.cancel(); await asyncio.gather(clock_task, return_exceptions=True)
        report = await runtime.close()
        output.update(events=events, saved=saved, tools=tool_names, wire_events=dict(wire_counts), api_errors=api_errors,
                      usage=report, failure=runtime.failure)
    assert report['gpt_final'], 'Live runtime did not capture final usage'


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=['vision', 'summary', 'client-audio', 'live-tools', 'live-photo', 'camera', 'all'], default='all')
    parser.add_argument('--image', type=Path, help='Explicitly authorized PNG/JPEG for --stage camera')
    args = parser.parse_args()
    if args.stage == 'camera' and not args.image:
        parser.error('--stage camera requires --image')
    key = read_key()
    await verify_openai_models(key, CONFIG)
    results = {'at': datetime.now(timezone.utc).isoformat(), 'synthetic_only': args.stage != 'camera',
               'config': CONFIG, 'checks': []}
    stages = {'vision': vision, 'summary': summary, 'client-audio': client_audio,
              'live-tools': live_tools, 'live-photo': live_tools}
    if args.stage == 'camera': stages = {'camera': camera}
    for name, action in stages.items():
        if args.stage not in ('all', name): continue
        result = {'stage': name}
        if name == 'camera': result['image_path'] = str(args.image.resolve())
        started = time.monotonic()
        print(json.dumps({'stage': name, 'status': 'running'}), flush=True)
        try:
            await asyncio.wait_for(action(key, result), 115)
            result['status'] = 'passed'
        except Exception as error:
            result.update(status='failed', error=error_info(error, key))
        result['elapsed_seconds'] = round(time.monotonic() - started, 3)
        results['checks'].append(result)
        path = ROOT / ('test-results/openai-probe-' + args.stage + '.json')
        path.write_text(json.dumps(results, ensure_ascii=False, indent=2).replace(key, '[REDACTED]'), encoding='utf-8')
        # The full local artifact retains evidence; terminal output remains compact.
        print(json.dumps({k: v for k, v in result.items() if k not in ('events', 'saved')},
                         ensure_ascii=False).replace(key, '[REDACTED]'), flush=True)
    return 0 if all(check['status'] == 'passed' for check in results['checks']) else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
