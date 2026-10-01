import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { electronTestFlags } from './electron-test-flags.mjs';
const app = await electron.launch({ args: ['.', '--background-diagnostic', ...electronTestFlags], cwd: process.cwd() });
const backend = process.argv.includes('--agy') ? 'agy' : 'codex';
try {
  const page = await app.firstWindow();
  const frames = [];
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('websocket', (socket) =>
    socket.on('framereceived', (frame) => {
      try {
        const event = JSON.parse(frame.payload.toString());
        if (event.type === 'transcript') frames.push({ type: event.type, text: event.text });
        if (event.type === 'speech')
          frames.push({
            type: event.type,
            text: event.text,
            boundaries: event.boundaries.length,
            audio_bytes: Math.floor(event.audio.length * 0.75),
          });
      } catch {}
    }),
  );
  await page.locator('summary').filter({ hasText: '對話方式' }).click();
  if (backend === 'agy') {
    await page.getByLabel('教學與作業看圖').selectOption('agy');
  }
  await page.getByRole('button', { name: '完整語音流程測試', exact: true }).click();
  await Promise.race([
    page.getByRole('button', { name: '插話：停止播放' }).waitFor({ timeout: 100000 }),
    page
      .getByRole('alert')
      .waitFor({ timeout: 100000 })
      .then(async () => {
        throw Error(await page.getByRole('alert').innerText());
      }),
  ]);
  assert.ok(frames.some((f) => f.type === 'transcript' && /[七7]/.test(f.text)));
  assert.ok(frames.some((f) => f.type === 'speech' && f.audio_bytes > 0 && f.boundaries > 0));
  await page.waitForTimeout(1000);
  await page.getByRole('button', { name: '插話：停止播放' }).click();
  assert.equal(errors.length, 0, errors.join('\n'));
  console.log(
    JSON.stringify({
      passed: [
        'synthetic audio source',
        'AudioWorklet capture and VAD',
        'Groq Whisper large v3',
        `${backend} structured tutoring`,
        'Edge TTS playback',
        'click interruption',
        'diagnostic cleanup',
      ],
      frames,
    }),
  );
} finally {
  await app.close();
}
