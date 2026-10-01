import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { electronTestFlags } from './electron-test-flags.mjs';
const app = await electron.launch({ args: ['.', '--background-diagnostic', ...electronTestFlags], cwd: process.cwd() });
try {
  const page = await app.firstWindow();
  await page.waitForFunction(() => !!window.desktop);
  const result = await page.evaluate(async () => {
    const connection = await window.desktop.connection();
    async function call(route, body) {
      const form = body instanceof FormData;
      const response = await fetch(connection.base + route, {
        method: 'POST',
        headers: {
          Authorization: 'Bearer ' + connection.token,
          ...(!form ? { 'Content-Type': 'application/json' } : {}),
        },
        body: form ? body : JSON.stringify(body),
      });
      if (!response.ok) {
        const error = await response.json();
        throw Error(error.detail);
      }
      return response.json();
    }
    const syntheticText = '七加五要怎麼算？請給我一點提示。';
    const speech = await call('/diagnostics/tts', { text: syntheticText });
    const bytes = Uint8Array.from(atob(speech.audio), (c) => c.charCodeAt(0));
    const form = new FormData();
    form.append('file', new Blob([bytes], { type: 'audio/mpeg' }), 'synthetic.mp3');
    const transcription = await call('/transcribe', form);
    return {
      model: 'whisper-large-v3',
      timeout_seconds: 30,
      source: 'synthetic Edge TTS, no microphone input',
      transcript: transcription.text,
      audio_bytes: bytes.length,
    };
  });
  assert.match(result.transcript, /[七7]/);
  assert.match(result.transcript, /[五5]/);
  console.log(JSON.stringify(result));
} finally {
  await app.close();
}
