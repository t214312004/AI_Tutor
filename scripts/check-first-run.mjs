import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { electronTestFlags } from './electron-test-flags.mjs';

await fs.mkdir('.local', { recursive: true });
const home = await fs.mkdtemp(path.resolve('.local/first-run-'));
const options = {
  args: ['.', '--test-mode', ...electronTestFlags, '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'],
  cwd: process.cwd(),
  env: { ...process.env, TUTOR_TEST_EMPTY: '1', TUTOR_TEST_HOME: home },
};
let app;
try {
  app = await electron.launch(options);
  let page = await app.firstWindow();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.getByRole('button', { name: '新增學生', exact: true }).waitFor();
  await page.waitForFunction(() => !document.querySelector('.student-create button[type=submit]')?.disabled ||
    document.querySelector('.start-card [role=status]')?.textContent?.includes('歡迎'));
  assert.equal(await page.locator('.students .student').count(), 0);
  assert.equal(await page.getByRole('button', { name: '開始專注工作區' }).isDisabled(), true);
  await page.getByLabel('新學生稱呼').fill('合成新學生');
  await page.getByLabel('新學生年級').selectOption('5');
  await page.getByRole('button', { name: '新增學生', exact: true }).click();
  await page.locator('.students').getByRole('button', { name: /合成新學生/ }).waitFor();
  await page.getByRole('button', { name: '開始專注工作區' }).click();
  await page.getByRole('button', { name: '結束', exact: true }).waitFor();
  await page.getByRole('button', { name: '結束', exact: true }).click();
  await page.getByRole('button', { name: '開始專注工作區' }).waitFor();
  await app.close();
  app = await electron.launch(options);
  page = await app.firstWindow();
  await page.locator('.students').getByRole('button', { name: /合成新學生/ }).waitFor();
  const history = await page.evaluate(async () => {
    const c = await window.desktop.connection();
    const headers = { Authorization: `Bearer ${c.token}` };
    const boot = await fetch(`${c.base}/bootstrap`, { headers }).then(r => r.json());
    return fetch(`${c.base}/students/${boot.students[0].id}/history`, { headers }).then(r => r.json());
  });
  assert.equal(history.sessions.length, 1);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: ['empty install', 'create student', 'offline workspace', 'restart keeps student and history'], privateDataUsed: false }));
} finally {
  if (app) await app.close();
  // Synthetic fixture stays under .local for local debugging, outside all exports.
}
