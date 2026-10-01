import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { electronTestFlags } from './electron-test-flags.mjs';

const app = await electron.launch({
  args: ['.', '--test-mode', ...electronTestFlags,
    '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'],
  cwd: process.cwd(),
});
try {
  const page = await app.firstWindow();
  page.setDefaultTimeout(15000);
  await page.getByRole('button', { name: '開始專注工作區' }).click();
  await page.getByRole('button', { name: '結束', exact: true }).waitFor();
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  let arrived;
  const requested = new Promise((resolve) => { arrived = resolve; });
  await page.route('**/sessions/*/finalize', async (route) => {
    arrived();
    await held;
    await route.continue();
  });
  await page.clock.install();
  await page.getByRole('button', { name: '結束', exact: true }).click();
  await requested;
  await page.getByRole('heading', { name: '正在保存本次陪讀紀錄' }).waitFor();
  assert.equal(await page.getByRole('button', { name: '開始專注工作區' }).count(), 0);
  assert.ok((await page.locator('.finalization-card').innerText()).includes('收音、拍照與播音已停止'));
  await page.clock.fastForward(31000);
  await page.getByText('仍在整理。收音、拍照與播音已停止；正在確認本次紀錄的保存狀態。').waitFor();
  const guarded = await app.evaluate(({ BrowserWindow, dialog }) => {
    const window = BrowserWindow.getAllWindows()[0];
    const previous = dialog.showMessageBoxSync;
    dialog.showMessageBoxSync = () => 0;
    try { window.close(); return !window.isDestroyed(); }
    finally { dialog.showMessageBoxSync = previous; }
  });
  assert.equal(guarded, true, 'window close should be blocked while finalizing');
  release();
  await page.getByRole('button', { name: '開始專注工作區' }).waitFor();
  const status = await page.evaluate(async () => {
    const connection = await window.desktop.connection();
    const response = await fetch(connection.base + '/students/student-2/history', {
      headers: { Authorization: 'Bearer ' + connection.token },
    });
    return (await response.json()).finalizations[0];
  });
  assert.equal(status.state, 'completed');
  assert.equal(status.summary, null);
  console.log(JSON.stringify({ passed: ['visible saving screen before completion',
    'long wait message after 30 seconds',
    'window close is guarded while finalizing',
    'returns home after durable offline finalization', 'does not claim offline AI analysis'] }));
} finally {
  await app.close();
}
