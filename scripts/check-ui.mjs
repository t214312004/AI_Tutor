import { _electron as electron } from 'playwright';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import { electronTestFlags } from './electron-test-flags.mjs';
await fs.mkdir('test-results', { recursive: true });
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
const results = [];
try {
  const page = await app.firstWindow();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.getByRole('button', { name: '開始專注工作區' }).waitFor();
  await page.getByRole('button', { name: '關閉鏡頭' }).waitFor();
  await page.waitForFunction(() => document.querySelector('video')?.videoWidth > 0);
  await page.getByLabel('旋轉角度').selectOption('90');
  await page.waitForFunction(() => {
    const canvas = document.querySelector('.preflight-frame canvas');
    return canvas?.width > 0 && canvas.width < canvas.height;
  });
  await page.getByLabel('旋轉角度').selectOption('180');
  await page.waitForFunction(() => {
    const canvas = document.querySelector('.preflight-frame canvas');
    return canvas?.width > canvas?.height;
  });
  await page.getByRole('button', { name: '調整拍攝範圍' }).click();
  await page.evaluate(() => window.scrollTo(0, 0));
  const firstCorner = page.getByRole('button', { name: '拍攝範圍第 1 角' });
  const firstBox = await firstCorner.boundingBox();
  const frameBox = await page.locator('.preflight-frame').boundingBox();
  await page.mouse.move(firstBox.x + firstBox.width / 2, firstBox.y + firstBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(frameBox.x + frameBox.width * 0.1, frameBox.y + frameBox.height * 0.1, {
    steps: 5,
  });
  await page.mouse.up();
  const keyboardBefore = await firstCorner.evaluate((element) =>
    parseFloat(getComputedStyle(element).left),
  );
  await firstCorner.focus();
  await page.keyboard.press('ArrowRight');
  const keyboardAfter = await firstCorner.evaluate((element) =>
    parseFloat(getComputedStyle(element).left),
  );
  assert.ok(keyboardAfter > keyboardBefore, 'keyboard calibration should move a corner');
  await page.getByRole('button', { name: '套用範圍' }).click();
  const stored = await page.evaluate(() => JSON.parse(localStorage.getItem('camera-settings-v2')));
  assert.ok(Object.values(stored).some((s) => s.corners[0][0] > 0.05 && s.corners[0][1] > 0.05));
  await page.getByRole('button', { name: '還原完整畫面' }).click();
  await page.locator('.preflight-optional > summary').click();
  await page.getByLabel('說話聲音').selectOption('zh-TW-HsiaoChenNeural');
  let requestedVoice = '';
  await page.route('**/diagnostics/tts', async (route) => {
    requestedVoice = route.request().postDataJSON().voice;
    await route.fulfill({ json: { audio: 'AA==', boundaries: [] } });
  });
  await page.evaluate(() => {
    window.__originalAudio = window.Audio;
    window.Audio = class extends window.Audio {
      play() {
        return Promise.resolve();
      }
    };
  });
  await page.getByRole('button', { name: '試聽老師聲音' }).click();
  await page.getByRole('button', { name: '停止試聽' }).waitFor();
  assert.equal(requestedVoice, 'zh-TW-HsiaoChenNeural');
  await page.getByRole('button', { name: '停止試聽' }).click();
  await page.evaluate(() => {
    window.Audio = window.__originalAudio;
  });
  await page.unroute('**/diagnostics/tts');
  await page.evaluate(() => {
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async (constraints) => {
      const stream = await original(constraints);
      if (constraints.audio && !constraints.video)
        window.__preflightMicTrack = stream.getAudioTracks()[0];
      return stream;
    };
  });
  await page.getByRole('button', { name: '測試麥克風' }).click();
  await page.getByRole('button', { name: '停止檢查' }).waitFor();
  await page.waitForFunction(() => window.__preflightMicTrack?.readyState === 'live');
  await page.getByRole('button', { name: '停止檢查' }).click();
  assert.equal(await page.evaluate(() => window.__preflightMicTrack.readyState), 'ended');
  await page.evaluate(() => {
    window.__cameraCallsAfterPreview = 0;
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = (constraints) => {
      if (constraints.video) window.__cameraCallsAfterPreview++;
      return original(constraints);
    };
  });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: 'test-results/home.png' });
  await page.getByRole('button', { name: '家長設定' }).click();
  await page.getByRole('tab', { name: '學校與課綱', exact: true }).click();
  await page.getByRole('heading', { name: '範例國小（虛構）' }).waitFor();
  await page.locator('.profile-selector').getByRole('button', { name: '測試同學' }).click();
  await page.getByRole('heading', { name: '尚未設定學校' }).waitFor();
  await page.getByText('請先到個人檔案設定這位學生的學校。').waitFor();
  await page.locator('.profile-selector').getByRole('button', { name: '小二同學' }).click();
  await page.getByRole('heading', { name: '範例國小（虛構）' }).waitFor();
  await page.screenshot({ path: 'test-results/parent.png' });
  await page.getByRole('button', { name: '查詢課綱', exact: true }).click();
  await page.locator('.curriculum-hits article').first().waitFor();
  await page.getByText('2 年級相關學習條目').waitFor();
  await page.screenshot({ path: 'test-results/curriculum-map.png' });
  await page.getByRole('button', { name: '關閉設定' }).click();
  assert.equal(
    await page.evaluate(
      () => document.querySelector('video').srcObject.getVideoTracks()[0].readyState,
    ),
    'live',
  );
  await page.locator('.students').getByRole('button', { name: /測試同學/ }).click();
  await page.getByLabel('說話聲音').selectOption('zh-TW-HsiaoChenNeural');
  let sessionVoice = '';
  page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/sessions')
      sessionVoice = request.postDataJSON().edge_voice;
  });
  await page.getByRole('button', { name: '開始專注工作區' }).click();
  await page.getByRole('button', { name: '關鏡頭', exact: true }).waitFor();
  assert.equal(sessionVoice, 'zh-TW-HsiaoChenNeural');
  assert.equal(
    await page.evaluate(() => window.__cameraCallsAfterPreview),
    0,
    'start should reuse preview stream',
  );
  await page.waitForFunction(() => document.querySelector('video')?.videoWidth > 0);
  const screens = await app.evaluate(({ BrowserWindow, screen }) => ({
    full: BrowserWindow.getAllWindows().filter((w) => w.isFullScreen()).length,
    displays: screen.getAllDisplays().length,
  }));
  assert.equal(screens.full, screens.displays);
  await page.screenshot({ path: 'test-results/session.png' });
  assert.equal(await page.getByRole('button', { name: '調整拍攝範圍' }).count(), 0);
  await page.getByRole('button', { name: '白板互動測試' }).click();
  await page.waitForTimeout(500);
  const board = await app.evaluate(({ webContents }) =>
    webContents
      .getAllWebContents()
      .filter((w) => w.getURL().startsWith('data:text/html'))
      .map((w) => ({
        url: w.getURL().slice(0, 40),
        preferences: w.getLastWebPreferences(),
        pid: w.getOSProcessId(),
      })),
  );
  assert.ok(board.length >= 1);
  assert.equal(board[0].preferences.nodeIntegration, false);
  assert.equal(board[0].preferences.sandbox, true);
  const mainPid = await app.evaluate(({ webContents }) =>
    webContents
      .getAllWebContents()
      .find((w) => w.getURL().startsWith('file:') && w.getURL().includes('dist/index.html'))
      .getOSProcessId(),
  );
  assert.notEqual(mainPid, board[0].pid, 'AI whiteboard must have a separate renderer process');
  await page.getByRole('button', { name: '收起白板內容' }).click();
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);
  assert.equal(
    await app.evaluate(({ BrowserWindow }) =>
      BrowserWindow.getAllWindows().some((w) => w.isFullScreen()),
    ),
    false,
  );
  await page.getByRole('button', { name: '關鏡頭', exact: true }).click();
  assert.equal(
    await page
      .locator('video')
      .evaluate(
        (v) => !v.srcObject || v.srcObject.getTracks().every((t) => t.readyState === 'ended'),
      ),
    true,
  );
  await page.getByRole('button', { name: '開鏡頭', exact: true }).click();
  await page.getByRole('button', { name: '關鏡頭', exact: true }).waitFor();
  const sessionTrack = await page.evaluate(
    () => document.querySelector('video').srcObject.getVideoTracks()[0].id,
  );
  await page.getByRole('button', { name: '結束', exact: true }).click();
  await page.getByRole('button', { name: '開始專注工作區' }).waitFor();
  await page.getByRole('button', { name: '關閉鏡頭' }).waitFor();
  assert.notEqual(
    await page.evaluate(() => document.querySelector('video').srcObject.getVideoTracks()[0].id),
    sessionTrack,
  );
  await page.locator('.preflight-frame canvas:not(.hidden)').waitFor();
  await page.getByRole('button', { name: '家長設定' }).click();
  await page.getByRole('tab', { name: '學習紀錄', exact: true }).click();
  await page.locator('.history-row').first().waitFor();
  await page.getByRole('button', { name: '關閉設定' }).click();
  for (const [width, height] of [
    [1366, 768],
    [1920, 1080],
  ]) {
    await app.evaluate(
      ({ BrowserWindow }, size) => {
        const main = BrowserWindow.getAllWindows().find((w) =>
          w.webContents.getURL().startsWith('file:'),
        );
        main.unmaximize();
        main.setSize(size[0], size[1]);
      },
      [width, height],
    );
    await page.waitForFunction((expected) => Math.abs(innerWidth - expected) < 30, width);
    const desktop = await page.evaluate(() => ({
      viewport: innerWidth,
      content: document.documentElement.scrollWidth,
      preview: document.querySelector('.preflight-display').getBoundingClientRect().width,
    }));
    assert.ok(desktop.content <= desktop.viewport + 2, JSON.stringify(desktop));
    assert.ok(desktop.preview > 500, JSON.stringify(desktop));
    await page.screenshot({ path: `test-results/preflight-${width}.png` });
  }
  await app.evaluate(({ BrowserWindow }) => {
    const main = BrowserWindow.getAllWindows().find((w) =>
      w.webContents.getURL().startsWith('file:'),
    );
    main.unmaximize();
    main.setSize(910, 620);
  });
  await page.waitForFunction(() => innerWidth < 1000);
  const compact = await page.evaluate(() => ({
    viewport: innerWidth,
    content: document.documentElement.scrollWidth,
    preview: document.querySelector('.preflight-display').getBoundingClientRect().width,
    start: document.querySelector('button.start').getBoundingClientRect().width,
  }));
  assert.ok(compact.content <= compact.viewport + 2, JSON.stringify(compact));
  assert.ok(compact.preview > 400 && compact.start > 250, JSON.stringify(compact));
  await page.screenshot({ path: 'test-results/preflight-compact.png', fullPage: true });
  assert.equal(errors.length, 0, errors.join('\n'));
  results.push(
    'desktop startup',
    'school context',
    'curriculum search',
    'all-display fullscreen entry/ESC exit',
    'fake camera single owner',
    'preflight perspective calibration',
    'selected teacher voice preview',
    'preflight microphone check releases its track',
    'separate-process isolated JS board',
    'camera release',
    'session persistence',
    '1366 and 1920 desktop preflight layout',
    'compact preflight without horizontal overflow',
    'no renderer exceptions',
  );
  console.log(JSON.stringify({ passed: results }, null, 2));
} finally {
  await app.close();
}
