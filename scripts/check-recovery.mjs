// Synthetic lesson: force a local WebSocket interruption and verify the same lesson resumes.
import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { electronTestFlags } from './electron-test-flags.mjs';

const backend = process.argv.includes('--agy') ? 'agy' : 'codex';
const checkPhoto = process.argv.includes('--photo');
const app = await electron.launch({
  args: ['.', '--test-mode', ...electronTestFlags, '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'],
  cwd: process.cwd(),
});
try {
  const page = await app.firstWindow();
  page.setDefaultTimeout(30000);
  const connection = await page.evaluate(() => window.desktop.connection());
  const credentialHost = await electron.launch({ args: ['electron/credential-host.cjs', ...electronTestFlags], cwd: process.cwd() });
  try {
    await credentialHost.evaluate(async (_, config) => {
      const saved = globalThis.readTutorCredentials();
      if (!saved.GROQ_API_KEY) throw Error('Groq key missing');
      const response = await fetch(config.base + '/credentials', {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + config.token, 'Content-Type': 'application/json' },
        body: JSON.stringify({ GROQ_API_KEY: saved.GROQ_API_KEY }),
      });
      if (!response.ok) throw Error('Local key setup failed');
    }, connection);
  } finally {
    await credentialHost.close();
  }
  await page.addInitScript(() => {
    const NativeSocket = window.WebSocket;
    window.__tutorTestSockets = [];
    window.WebSocket = class extends NativeSocket {
      constructor(...args) {
        super(...args);
        window.__tutorTestSockets.push(this);
      }
    };
  });
  const ids = [];
  page.on('websocket', (socket) => socket.on('framereceived', (frame) => {
    try {
      const event = JSON.parse(frame.payload.toString());
      if (event.type === 'ready') ids.push(event.session_id);
    } catch {}
  }));
  await page.reload();
  if (backend === 'agy') {
    await page.locator('summary').filter({ hasText: '對話方式' }).click();
    await page.getByLabel('教學與作業看圖').selectOption('agy');
  }
  await page.locator('textarea').first().fill('合成斷線恢復測試，沒有真實學生資料。只做數學 7＋5。');
  await page.getByRole('button', { name: '檢查並開始陪讀' }).click();
  await page.getByText('AI 陪讀已連線').waitFor({ timeout: 90000 });
  await page.getByRole('button', { name: '關閉麥克風', exact: true }).waitFor();
  const photoVersion = async () => app.evaluate((_, sessionId) => {
    const fs = process.getBuiltinModule('fs');
    const path = process.getBuiltinModule('path');
    const metadata = path.join(process.cwd(), '.local/electron-test/data/runtime/student-2',
      'basic-shared/teacher/internal/latest-photo.json');
    return fs.existsSync(metadata) ? JSON.parse(fs.readFileSync(metadata, 'utf8')).version : null;
  }, ids[0]);
  let firstPhotoVersion = null;
  if (checkPhoto) {
    await page.waitForFunction(() => /每 10 秒擷取 · 共 [1-9]\d* 張/.test(document.body.innerText),
      null, { timeout: 30000 });
    for (let attempt = 0; attempt < 30 && firstPhotoVersion === null; attempt++) {
      firstPhotoVersion = await photoVersion();
      if (firstPhotoVersion === null) await page.waitForTimeout(100);
    }
    assert.ok(firstPhotoVersion !== null, 'first photo must exist before interruption');
  }
  await page.evaluate(() => window.__tutorTestSockets[0].close(4000, 'synthetic-probe'));
  await page.waitForFunction(() => window.__tutorTestSockets.length >= 2, null, { timeout: 30000 });
  await page.getByText('AI 陪讀已連線').waitFor({ timeout: 45000 });
  await page.getByRole('button', { name: '關閉麥克風', exact: true }).waitFor({ timeout: 30000 });
  if (checkPhoto) {
    let current = await photoVersion();
    for (let attempt = 0; attempt < 120 && current <= firstPhotoVersion; attempt++) {
      await page.waitForTimeout(100);
      current = await photoVersion();
    }
    assert.ok(current > firstPhotoVersion, 'camera should refresh the preserved photo');
  }
  await page.getByRole('textbox', { name: '用文字求助' }).fill('七加五要怎麼想？請只給我一小步提示。');
  await page.getByRole('button', { name: '送出文字' }).click();
  await page.getByText(/你說：七加五要怎麼想/).waitFor({ timeout: 90000 });
  await page.waitForFunction(() => document.querySelectorAll('.assistant-row .bubble').length >= 3, null, { timeout: 120000 });
  assert.equal(ids.length, 2);
  assert.equal(ids[0], ids[1], 'reconnect must resume the same lesson');
  await page.getByRole('button', { name: '結束', exact: true }).click();
  console.log(JSON.stringify({ backend, recovered: true, same_session: true, photo_refreshed: checkPhoto }));
} finally {
  await app.close();
}
