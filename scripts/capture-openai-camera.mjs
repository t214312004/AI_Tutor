// User-authorized real camera capture in an isolated test profile; no lesson is started.
import { _electron as electron } from 'playwright';
import fs from 'node:fs/promises';
import { electronTestFlags } from './electron-test-flags.mjs';

const app = await electron.launch({ args: ['.', '--test-mode', ...electronTestFlags], cwd: process.cwd() });
try {
  const page = await app.firstWindow();
  await page.waitForFunction(() => [...document.querySelectorAll('video')].some(v => v.videoWidth > 0 && v.readyState >= 2), { timeout: 20000 });
  await page.waitForTimeout(2500); // Let the camera's automatic exposure settle.
  const capture = await page.evaluate(async () => {
    const video = [...document.querySelectorAll('video')].find(v => v.videoWidth > 0 && v.readyState >= 2);
    const raw = document.createElement('canvas');
    raw.width = video.videoWidth; raw.height = video.videoHeight;
    raw.getContext('2d').drawImage(video, 0, 0);
    const preview = document.querySelector('.preflight-frame canvas');
    const track = video.srcObject.getVideoTracks()[0];
    return { raw: raw.toDataURL('image/jpeg', .95), preview: preview?.toDataURL('image/jpeg', .95),
      width: raw.width, height: raw.height, camera: track.label };
  });
  await fs.mkdir('.local/openai-camera-probe', { recursive: true });
  for (const name of ['raw', 'preview']) {
    if (capture[name]) await fs.writeFile(`.local/openai-camera-probe/${name}.jpg`, Buffer.from(capture[name].split(',')[1], 'base64'));
  }
  console.log(JSON.stringify({ width: capture.width, height: capture.height, camera: capture.camera,
    raw: '.local/openai-camera-probe/raw.jpg', preview: capture.preview ? '.local/openai-camera-probe/preview.jpg' : null }));
} finally {
  await app.close();
}
