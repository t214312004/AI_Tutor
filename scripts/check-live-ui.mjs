import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { electronTestFlags } from './electron-test-flags.mjs';
const nativeCamera = process.argv.includes('--native-camera');
const app = await electron.launch({
  args: [
    '.',
    '--test-mode',
    ...electronTestFlags,
    '--use-fake-ui-for-media-stream',
    ...(nativeCamera ? ['--native-camera-probe'] : ['--use-fake-device-for-media-stream']),
  ],
  cwd: process.cwd(),
});
try {
  const page = await app.firstWindow();
  page.setDefaultTimeout(15000);
  await page.getByRole('button', { name: '家長設定' }).waitFor();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.evaluate(async () => {
    const config = await window.desktop.connection();
    await fetch(config.base + '/credentials', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + config.token, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        OPENAI_API_KEY: 'test-only-not-a-real-key',
        GEMINI_API_KEY: 'test-only-not-a-real-key',
      }),
    });
  });
  // Mock session setup too: configured GPT lessons now verify real model access.
  // This renderer test must never query a provider with its synthetic key.
  await page.route('**/gpt-options', route => route.fulfill({ json: {
    openai: { models: [
      { id: 'gpt-6.1-sol', label: 'gpt-6.1-sol', efforts: ['low'], image: true },
      { id: 'gpt-6-luna', label: 'gpt-6-luna', efforts: ['max'], image: true },
    ], error: null },
    codex: { models: [], error: 'mock' }, agy: { models: [], error: 'mock' },
  } }));
  let sessionSequence = 0;
  await page.route(/\/sessions(?:\/|$)/, route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === '/sessions') return route.fulfill({ json: {
      id: `mock-live-${++sessionSequence}`, online: true,
    } });
    const id = pathname.split('/')[2];
    if (pathname.endsWith('/frame')) {
      const frame = route.request().postDataJSON();
      return (async () => {
        await page.evaluate(frame => {
          window.mockEvents.push({ ...frame, type: 'frame', transport: 'http' });
          window.uploadAudioBefore = window.mockEvents.filter(e => e.type === 'audio').length;
        }, frame);
        // An in-flight photo must not stop PCM capture or use the audio socket.
        await page.waitForTimeout(1000);
        await page.evaluate(() => {
          window.uploadAudioAfter = window.mockEvents.filter(e => e.type === 'audio').length;
        });
        await route.fulfill({ json: { accepted: true } });
      })();
    }
    return route.fulfill({ json: pathname.includes('/finaliz') ? {
      session_id: id, state: 'completed', stage: 'completed',
      snapshot_saved: true, summary_available: true, online: true,
    } : {} });
  });
  await page.evaluate(() => {
    // A reusable test profile may still hold a retired model or previous crop.
    for (const key of Object.keys(localStorage))
      if (key.startsWith('gpt-live-config:')) localStorage.removeItem(key);
    localStorage.removeItem('camera-settings-v2');
    localStorage.removeItem('camera-device-id');
  });
  await page.addInitScript(nativeCamera => {
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    if (nativeCamera) {
      // Exercise actual photography with synthetic microphone input; keep real
      // worksheet pixels in memory and never send them to an external provider.
      navigator.mediaDevices.getUserMedia = async constraints => {
        if (constraints.video) return original(constraints);
        const context = new AudioContext();
        await context.resume();
        const oscillator = context.createOscillator();
        const gain = context.createGain(); gain.gain.value = 0.08;
        const destination = context.createMediaStreamDestination();
        oscillator.connect(gain).connect(destination); oscillator.start();
        return destination.stream;
      };
      return;
    }
    const pattern = document.createElement('canvas'); pattern.width = 1920; pattern.height = 1080;
    const ctx = pattern.getContext('2d');
    const pixels = ctx.createImageData(1920, 1080);
    let seed = 123456;
    for (let i = 0; i < pixels.data.length; i += 4) {
      for (let c = 0; c < 3; c++) {
        seed = (Math.imul(seed,1664525) + 1013904223) >>> 0;
        pixels.data[i + c] = seed >>> 24;
      }
      pixels.data[i + 3] = 255;
    }
    ctx.putImageData(pixels,0,0);
    window.nativePhotoFixture = pattern.toDataURL('image/png');
    const stream = pattern.captureStream(10);
    setInterval(() => ctx.putImageData(pixels,0,0),100);
    navigator.mediaDevices.getUserMedia = async constraints => {
      if (!constraints.video) return original(constraints);
      const preview = stream.clone();
      if (window.delayNextPreview) {
        window.delayNextPreview = false;
        window.previewRestoreWaiting = true;
        await new Promise(resolve => { window.releasePreviewRestore = resolve; });
      }
      return preview;
    };
  }, nativeCamera);
  await page.reload();
  await page.getByRole('button', { name: '家長設定' }).waitFor();
  if (!nativeCamera) await page.evaluate(() => window.desktop.setTestPhoto(window.nativePhotoFixture));
  await page.evaluate(() => {
    window.playedChunks = 0;
    window.mockEvents = [];
    const original = AudioBufferSourceNode.prototype.start;
    AudioBufferSourceNode.prototype.start = function (...args) {
      window.playedChunks++;
      return original.apply(this, args);
    };
    window.WebSocket = class {
      static OPEN = 1;
      bufferedAmount = 0;
      readyState = 0;
      constructor(url) {
        this.id = url.split('/').at(-2);
        window.mockSocket = this;
        setTimeout(() => {
          this.readyState = 1;
          this.onopen?.();
        }, 0);
      }
      emit(data) {
        this.onmessage?.({ data: JSON.stringify(data) });
      }
      send(raw) {
        const message = JSON.parse(raw);
        if (message.type === 'authenticate')
          this.emit({ type: 'ready', session_id: this.id, generation: 0 });
        else {
          if (message.type === 'frame') {
            this.bufferedAmount = raw.length;
            setTimeout(() => { this.bufferedAmount = 0; },1000);
          }
          window.mockEvents.push(message.type === 'audio' ? { type: 'audio' } : message);
          if (message.type === 'close') this.emit({ type: 'closed', cost: { test: true } });
        }
      }
      close() {
        this.readyState = 3;
        this.onclose?.();
      }
    };
  });
  const nativeResults = [];
  for (const mode of nativeCamera ? ['GPT Live'] : ['GPT Live', 'Gemini Live']) {
    await page.evaluate(() => (window.mockEvents = []));
    await page.locator('.advanced > summary').click();
    await page.getByRole('button', { name: mode, exact: false }).click();
    try {
      await page.getByRole('button', { name: '檢查並開始陪讀', exact: true }).click();
    } catch (error) {
      throw Error(`${mode}: ${await page.locator('main').innerText()}`, { cause: error });
    }
    await page.getByText('AI 陪讀已連線', { exact: true }).waitFor();
    // Exercise the actual lesson capture callback and scheduled uploads.
    await page.evaluate(() => window.mockSocket.emit({
      type: 'capture_requested', request_id: 'native-photo-check',
    }));
    await page.waitForFunction(() => window.mockEvents.some(e =>
      e.type === 'frame' && e.request_id === 'native-photo-check'));
    const photo = await page.evaluate(async () => {
      const frame = window.mockEvents.find(e => e.type === 'frame' && e.request_id === 'native-photo-check');
      const image = new Image(); image.src = frame.image; await image.decode();
      return { png: frame.image.startsWith('data:image/png;base64,'),
        chars: frame.image.length, transport: frame.transport,
        width: image.width, height: image.height, meta: frame.capture_meta };
    });
    assert.equal(photo.png, mode === 'GPT Live', 'OpenAI capture uses lossless PNG');
    assert.equal(photo.meta.capture_method, nativeCamera ? 'windows-native-photo' : 'test-fixture');
    if (mode === 'GPT Live') {
      assert.equal(photo.transport,'http');
      assert.ok(photo.chars > 2_000_000, 'test PNG must exceed the old microphone cutoff');
      assert.equal(photo.width, photo.meta.source_width);
      assert.equal(photo.height, photo.meta.source_height);
      await page.waitForFunction(() => window.uploadAudioAfter > window.uploadAudioBefore);
      await page.waitForFunction(() => window.mockEvents.some(e =>
        e.type === 'frame' && !e.request_id && e.image.startsWith('data:image/png;base64,')));
      if (!nativeCamera) {
        await page.evaluate(() => {
          window.delayNextPreview = true;
          window.mockSocket.emit({ type: 'capture_requested', request_id: 'cancel-during-restore' });
        });
        await page.waitForFunction(() => window.previewRestoreWaiting);
        await page.getByRole('button', { name: '關鏡頭', exact: true }).click();
        await page.evaluate(() => window.releasePreviewRestore());
        await page.waitForTimeout(100);
        assert.equal(await page.evaluate(() => window.mockEvents.some(e => e.request_id === 'cancel-during-restore')), false);
        await page.getByRole('button', { name: '開鏡頭', exact: true }).click();
        await page.waitForFunction(() => document.querySelector('video')?.videoWidth > 0);
      }
      if (nativeCamera) {
        await page.waitForFunction(() => window.mockEvents.filter(e => e.type === 'frame' && !e.request_id).length >= 2);
        const info = await page.evaluate(() => ({
          shots: window.mockEvents.filter(e => e.type === 'frame').map(e => ({
            width: e.capture_meta.source_width, height: e.capture_meta.source_height,
            method: e.capture_meta.capture_method, at: e.capture_meta.captured_at,
          })),
          previewRestored: document.querySelector('video')?.videoWidth > 0,
          cameraOff: window.mockEvents.some(e => e.type === 'camera' && !e.enabled),
        }));
        assert.ok(info.previewRestored);
        assert.equal(info.cameraOff, false, 'temporary preview release must not disable observations');
        const periodic = info.shots.slice(-2);
        const interval = Date.parse(periodic[1].at) - Date.parse(periodic[0].at);
        assert.ok(interval > 8500 && interval < 11500, `native periodic interval: ${interval}`);
        await page.evaluate(() => {
          window.audioBeforePhoto = window.mockEvents.filter(e => e.type === 'audio').length;
          window.mockSocket.emit({ type: 'capture_requested', request_id: 'cancel-native-photo' });
        });
        await page.waitForFunction(() => !document.querySelector('video')?.srcObject);
        await page.getByRole('button', { name: '關鏡頭', exact: true }).click();
        await page.waitForTimeout(800);
        assert.equal(await page.evaluate(() => window.mockEvents.some(e => e.request_id === 'cancel-native-photo')), false);
        assert.ok(await page.evaluate(() => window.mockEvents.filter(e => e.type === 'audio').length > window.audioBeforePhoto));
        await page.getByRole('button', { name: '開鏡頭', exact: true }).click();
        await page.waitForFunction(() => document.querySelector('video')?.videoWidth > 0);
        nativeResults.push({ ...info, interval_ms: interval, cancel_and_reopen: true });
      }
    }
    await page.evaluate(() => {
      window.mockSocket.emit({
        type: 'transcript',
        role: 'user',
        transcript_id: 'turn-1',
        text: '七加',
      });
      window.mockSocket.emit({
        type: 'transcript',
        role: 'user',
        transcript_id: 'turn-1',
        text: '七加五？',
      });
    });
    await page.waitForFunction(() => {
      const messages = document.querySelectorAll('.messages .student-message');
      return messages.length === 1 && messages[0].textContent.includes('你說：七加五？');
    });
    await page.getByRole('button', { name: '關閉麥克風', exact: true }).waitFor();
    await page.waitForFunction(() => window.mockEvents.some((e) => e.type === 'audio'));
    const before = await page.evaluate(() => window.playedChunks);
    await page.evaluate(() =>
      window.mockSocket.emit({ type: 'pcm', audio: btoa('\0'.repeat(4800)), rate: 24000 }),
    );
    await page.waitForFunction((count) => window.playedChunks > count, before);
    await page.getByRole('button', { name: '關閉麥克風', exact: true }).click();
    await page.waitForTimeout(250);
    const muted = await page.evaluate(
      () => window.mockEvents.filter((e) => e.type === 'audio').length,
    );
    await page.waitForTimeout(250);
    assert.equal(
      await page.evaluate(() => window.mockEvents.filter((e) => e.type === 'audio').length),
      muted,
      'mute stops network audio',
    );
    assert.ok(
      await page.evaluate(() => window.mockEvents.some((e) => e.type === 'mic' && !e.enabled)),
    );
    if (nativeCamera) {
      await page.evaluate(() => window.mockSocket.emit({ type: 'capture_requested', request_id: 'cancel-at-end' }));
      await page.waitForFunction(() => !document.querySelector('video')?.srcObject);
    }
    await page.getByRole('button', { name: '結束', exact: true }).click();
    await page.getByRole('button', { name: '檢查並開始陪讀', exact: true }).waitFor({ state: 'visible' });
    await page.waitForFunction(() => !document.querySelector('button.start')?.disabled);
    assert.ok(await page.evaluate(() => window.mockEvents.some((e) => e.type === 'close')));
    if (nativeCamera) assert.equal(await page.evaluate(() => window.mockEvents.some(e => e.request_id === 'cancel-at-end')), false);
  }
  assert.deepEqual(errors, []);
  console.log(
    JSON.stringify({
      passed: [
        nativeCamera ? 'GPT session activation with actual native camera' : 'GPT and Gemini session activation',
        'OpenAI requested and periodic photos use native-resolution PNG',
        'large PNG HTTP upload does not interrupt WebSocket PCM capture',
        'cumulative transcript stays in one message',
        'streaming PCM capture',
        '24 kHz PCM playback',
        'mute stops upload',
        'close acknowledgement',
        'no renderer errors',
      ],
      provider: 'mock; no external API calls',
      ...(nativeCamera ? { native_camera: nativeResults } : {}),
    }),
  );
} finally {
  await app.close();
}
