import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { electronTestFlags } from './electron-test-flags.mjs';
const app = await electron.launch({
  args: [
    '.',
    '--test-mode',
    ...electronTestFlags,
    '--use-fake-ui-for-media-stream',
    '--use-fake-device-for-media-stream',
  ],
  cwd: process.cwd(),
});
try {
  const page = await app.firstWindow();
  page.setDefaultTimeout(15000);
  await page.getByRole('button', { name: '開始專注工作區' }).waitFor();
  await page.addInitScript(() => {
    const Original = window.Audio;
    window.__audioPlayers = 0;
    window.Audio = class extends Original {
      constructor(...args) {
        super(...args);
        window.__audioPlayers++;
      }
    };
  });
  await page.reload();
  let release;
  const delayed = new Promise((resolve) => (release = resolve));
  let requested;
  const arrived = new Promise((resolve) => (requested = resolve));
  await page.route('**/diagnostics/tts', async (route) => {
    requested();
    await delayed;
    await route.fulfill({ json: { audio: '', boundaries: [] } });
  });
  await page.locator('.preflight-optional > summary').click();
  await page.getByRole('button', { name: '試聽老師聲音' }).click();
  await Promise.race([
    arrived,
    new Promise((_, reject) =>
      setTimeout(() => reject(Error('TTS request was not intercepted')), 8000),
    ),
  ]);
  await page.locator('.advanced > summary').click();
  await page.getByRole('button', { name: 'GPT Live', exact: false }).click();
  release();
  await page.waitForTimeout(200);
  assert.equal(
    await page.evaluate(() => window.__audioPlayers),
    0,
    'changing mode must invalidate late TTS',
  );
  await page.getByRole('button', { name: '基本陪讀', exact: false }).click();
  await page.getByRole('button', { name: '開始專注工作區' }).click();
  await page.getByRole('button', { name: '關鏡頭', exact: true }).waitFor();
  await app.evaluate(({ powerMonitor }) => powerMonitor.emit('lock-screen'));
  await page.getByRole('button', { name: '開鏡頭', exact: true }).waitFor();
  assert.equal(
    await page
      .locator('video')
      .evaluate(
        (v) => !v.srcObject || v.srcObject.getTracks().every((t) => t.readyState === 'ended'),
      ),
    true,
  );
  assert.equal(
    await app.evaluate(({ BrowserWindow }) =>
      BrowserWindow.getAllWindows().some((w) => w.isFullScreen()),
    ),
    false,
  );
  await page.evaluate(async () => {
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    const camera = await original({ video: true, audio: false });
    const microphone = await original({ video: false, audio: true });
    window.__cameraRequests = 0;
    window.__micRequests = 0;
    window.__releaseCamera = null;
    window.__releaseMic = null;
    window.__testCamera = camera;
    window.__testMic = microphone;
    window.__originalGetUserMedia = original;
    navigator.mediaDevices.getUserMedia = (constraints) =>
      new Promise((resolve) => {
        if (constraints.video) {
          window.__cameraRequests++;
          window.__releaseCamera = () => resolve(camera);
        } else {
          window.__micRequests++;
          window.__releaseMic = () => resolve(microphone);
        }
      });
  });
  await page.getByRole('button', { name: '開鏡頭', exact: true }).click();
  await page.getByRole('button', { name: '開鏡頭', exact: true }).click();
  assert.equal(await page.evaluate(() => window.__cameraRequests), 1);
  await page.evaluate(() => window.__releaseCamera());
  await page.waitForFunction(() => window.__testCamera.getTracks()[0].readyState === 'ended');
  await page.getByRole('button', { name: '開鏡頭', exact: true }).waitFor();
  await page.evaluate(async () => {
    window.__activeCamera = await window.__originalGetUserMedia({ video: true, audio: false });
    navigator.mediaDevices.getUserMedia = (constraints) =>
      constraints.video
        ? Promise.resolve(window.__activeCamera)
        : new Promise((resolve) => {
            window.__micRequests++;
            window.__releaseMic = () => resolve(window.__testMic);
          });
  });
  await page.getByRole('button', { name: '開鏡頭', exact: true }).click();
  await page.getByRole('button', { name: '關鏡頭', exact: true }).waitFor();
  await page.getByRole('button', { name: '開啟麥克風', exact: true }).click();
  await page.getByRole('button', { name: '開啟麥克風', exact: true }).click();
  assert.equal(await page.evaluate(() => window.__micRequests), 1);
  await page.evaluate(() => window.__releaseMic());
  await page.getByRole('button', { name: '關閉麥克風', exact: true }).waitFor();
  await page.getByRole('button', { name: '結束', exact: true }).click();
  await page.getByRole('button', { name: '開始專注工作區' }).waitFor();
  assert.equal(await page.evaluate(() => window.__testCamera.getTracks()[0].readyState), 'ended');
  assert.equal(await page.evaluate(() => window.__activeCamera.getTracks()[0].readyState), 'ended');
  assert.equal(await page.evaluate(() => window.__testMic.getTracks()[0].readyState), 'ended');
  await page.evaluate(async () => {
    const config = await window.desktop.connection();
    await fetch(config.base + '/credentials', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + config.token, 'Content-Type': 'application/json' },
      body: JSON.stringify({ OPENAI_API_KEY: 'test-only-not-a-real-key' }),
    });
  });
  await page.reload();
  await page.locator('.advanced > summary').click();
  await page.getByRole('button', { name: 'GPT Live', exact: false }).click();
  await page.getByRole('button', { name: '檢查並開始陪讀', exact: true }).waitFor();
  await page.getByRole('button', { name: '關閉鏡頭' }).click();
  await page.evaluate(async () => {
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    const camera = await original({ video: true, audio: false });
    window.__lateCamera = camera;
    window.__webSockets = 0;
    window.WebSocket = class {
      constructor() {
        window.__webSockets++;
      }
    };
    navigator.mediaDevices.getUserMedia = (constraints) =>
      constraints.video
        ? new Promise((resolve) => {
            window.__releaseLateCamera = () => resolve(camera);
          })
        : original(constraints);
  });
  await page.getByRole('button', { name: '開啟鏡頭' }).click();
  await page.getByRole('button', { name: '檢查並開始陪讀', exact: true }).click();
  await page.getByRole('button', { name: '結束', exact: true }).waitFor();
  await page.getByRole('button', { name: '結束', exact: true }).click();
  await page.getByRole('button', { name: '檢查並開始陪讀', exact: true }).waitFor();
  await page.evaluate(() => window.__releaseLateCamera());
  await page.waitForFunction(() => window.__lateCamera.getTracks()[0].readyState === 'ended');
  assert.equal(await page.evaluate(() => window.__webSockets), 0);
  let transcriptionRequests = 0;
  await page.route('**/transcribe', async (route) => {
    transcriptionRequests++;
    await route.abort();
  });
  await page.evaluate(async () => {
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    const microphone = await original({ audio: true, video: false });
    window.__lateNoteMic = microphone;
    navigator.mediaDevices.getUserMedia = (constraints) =>
      constraints.audio
        ? new Promise((resolve) => {
            window.__releaseLateNote = () => resolve(microphone);
          })
        : original(constraints);
  });
  await page.getByRole('button', { name: '用說的', exact: true }).click();
  assert.equal(await page.locator('button.start').isDisabled(), true);
  await app.evaluate(({ powerMonitor }) => powerMonitor.emit('lock-screen'));
  await page.getByText('裝置已因鎖定或休眠而停止。', { exact: false }).waitFor();
  await page.evaluate(() => window.__releaseLateNote());
  await page.waitForFunction(() => window.__lateNoteMic.getTracks()[0].readyState === 'ended');
  assert.equal(transcriptionRequests, 0);
  assert.equal(await page.locator('button.start').isDisabled(), false);
  await page.reload();
  let releaseSession;
  const sessionHeld = new Promise((resolve) => (releaseSession = resolve));
  let sessionReady;
  const sessionArrived = new Promise((resolve) => (sessionReady = resolve));
  let staleSessionId;
  await page.route('**/sessions', async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    const response = await route.fetch();
    staleSessionId = (await response.json()).id;
    sessionReady();
    await sessionHeld;
    await route.fulfill({ response });
  });
  await page.getByRole('button', { name: '開始專注工作區' }).click();
  await sessionArrived;
  await app.evaluate(({ powerMonitor }) => powerMonitor.emit('lock-screen'));
  releaseSession();
  await page.getByRole('button', { name: '開始專注工作區' }).waitFor();
  await page.waitForFunction(async (id) => {
    const config = await window.desktop.connection();
    const response = await fetch(`${config.base}/students/student-2/history`, {
      headers: { Authorization: `Bearer ${config.token}` },
    });
    return (await response.json()).sessions.some(
      (session) => session.id === id && session.status === 'ended',
    );
  }, staleSessionId);
  console.log(
    JSON.stringify({
      passed: [
        'late TTS discarded after changing mode',
        'simulated lock releases camera',
        'simulated lock exits all fullscreen windows',
        'session can end after suspend',
        'cancelled camera permission closes its late track, and active tracks close on end',
        'ending during device permission prevents a late teaching connection',
        'suspend discards a pending voice note without transcription',
        'suspend during session creation closes the late session',
      ],
    }),
  );
} finally {
  await app.close();
}
