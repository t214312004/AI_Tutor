// Real selected CLI + Edge TTS; isolated test students, fake devices, no household media.
import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { electronTestFlags } from './electron-test-flags.mjs';
const backend = process.argv.includes('--agy') ? 'agy' : 'codex';
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
let page;
try {
  page = await app.firstWindow();
  page.setDefaultTimeout(15000);
  await page.getByRole('button', { name: '家長設定' }).waitFor();
  const connection = await page.evaluate(() => window.desktop.connection());
  // Decrypt in the Electron main process and send directly to our local core.
  // The key is never returned, printed, placed in a URL, or saved in test files.
  const credentialHost = await electron.launch({
    args: ['electron/credential-host.cjs', ...electronTestFlags],
    cwd: process.cwd(),
  });
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
  const events = [];
  let sessionId = '';
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('websocket', (socket) =>
    socket.on('framereceived', (frame) => {
      const event = JSON.parse(frame.payload.toString());
      if (event.type === 'ready') sessionId = event.session_id;
      if (event.type === 'speech') events.push({ type: 'speech', text: event.text });
    }),
  );
  await page.reload();
  if (backend === 'agy') {
    await page.locator('summary').filter({ hasText: '對話方式' }).click();
    await page.getByLabel('教學與作業看圖').selectOption('agy');
  }
  await page
    .locator('textarea')
    .first()
    .waitFor({ timeout: 120000 });
  await page
    .locator('textarea')
    .first()
    .fill('合成測試，沒有真實學生資料。本次只做數學練習 7+5 這一題，請先確認作業清單。');
  await page.getByRole('button', { name: '檢查並開始陪讀' }).click();
  await page.getByRole('button', { name: '關鏡頭', exact: true }).click();
  await page
    .getByRole('button', { name: '確認這份清單', exact: true })
    .waitFor({ timeout: 110000 });
  await page.getByRole('button', { name: '確認這份清單', exact: true }).click();
  await page.waitForFunction(
    () => document.querySelector('.lesson-list input')?.disabled === false,
  );
  // Wait for the opening audio to actually start before interrupting with a question.
  const interrupt = page.getByRole('button', { name: '打斷老師並說話', exact: true });
  await interrupt.waitFor({ timeout: 60000 });
  await page
    .getByRole('textbox', { name: '用文字求助' })
    .fill('七加五要怎麼算？給我一點提示，不要直接說答案。');
  await page.getByRole('button', { name: '送出文字', exact: true }).click();
  await interrupt.waitFor({ state: 'hidden' });
  await interrupt.waitFor({ timeout: 110000 });
  await interrupt.waitFor({ state: 'hidden', timeout: 45000 });
  assert.ok(events.length >= 2);
  const board = await app.evaluate(({ webContents }) =>
    webContents
      .getAllWebContents()
      .filter((w) => w.getURL().startsWith('data:text/html'))
      .map((w) => w.id),
  );
  if (board.length) {
    await app.evaluate(async ({ webContents }, id) => {
      const png = await webContents.fromId(id).capturePage();
      process
        .getBuiltinModule('fs')
        .writeFileSync(
          process.getBuiltinModule('path').join(process.cwd(), 'test-results/basic-whiteboard.png'),
          png.toPNG(),
        );
    }, board[0]);
  }
  assert.ok(await page.locator('.student-message').count());
  assert.ok(
    await page.getByRole('button', { name: '開啟麥克風', exact: true }).count(),
    'typing must mute the microphone',
  );
  const confirm = page.getByRole('button', { name: '確認這份清單', exact: true });
  if (await confirm.count()) await confirm.click();
  for (const checkbox of await page.locator('.lesson-list input').all()) await checkbox.check();
  await page.getByText('今天的清單已完成，可以按「結束」', { exact: true }).waitFor();
  await page.screenshot({ path: 'test-results/basic-lesson.png' });
  await app.evaluate(async ({ BrowserWindow }) => {
    const main = BrowserWindow.getAllWindows().find((w) =>
      w.webContents.getURL().startsWith('file:'),
    );
    main.setFullScreen(false);
    main.setSize(1100, 760);
  });
  await page.waitForTimeout(300);
  const layout = await page.evaluate(() => ({
    height: innerHeight,
    controls: [...document.querySelectorAll('.controls button')].map((e) => ({
      bottom: e.getBoundingClientRect().bottom,
      top: e.getBoundingClientRect().top,
    })),
    checkbox: document.querySelector('.lesson-list input')?.getBoundingClientRect().width,
  }));
  assert.ok(layout.controls.every((b) => b.top >= 0 && b.bottom <= layout.height));
  assert.ok(layout.checkbox <= 20);
  await page.screenshot({ path: 'test-results/basic-lesson-compact.png' });
  await page.getByRole('button', { name: '結束', exact: true }).click();
  await page.getByRole('button', { name: '檢查並開始陪讀' }).waitFor({ timeout: 120000 });
  await page.waitForFunction(() => !document.querySelector('button.start')?.disabled);
  const history = await page.evaluate(async () => {
    const c = await window.desktop.connection();
    return (
      await fetch(c.base + '/students/student-2/history', {
        headers: { Authorization: 'Bearer ' + c.token },
      })
    ).json();
  });
  const summary = history.events.find(
    (e) => e.session_id === sessionId && e.kind === 'learning_summary',
  )?.payload;
  assert.ok(summary?.tasks.length);
  assert.ok(summary.tasks.every((t) => t.status === 'done'));
  assert.ok(summary.heard_guidance.length);
  assert.ok(history.learning_context.some((e) => e.session_id === sessionId));
  assert.ok(history.learning_evidence.some((e) =>
    e.session_id === sessionId && e.kind === 'asked_for_help' && e.code === 'N-1-3'));
  assert.ok(history.learning_profile.some((e) =>
    e.code === 'N-1-3' && e.parent_status === 'unassessed'));
  assert.deepEqual(errors, []);
  console.log(
    JSON.stringify({
      passed: [
        'real opening and proposed task list',
        'explicit plan confirmation',
        'text interruption mutes the microphone',
        `real ${backend} teaching turn and Edge TTS`,
        'multi-turn transcript',
        'completion checklist',
        'summary saved and loaded for next session',
        'source-backed help retained without automatic mastery',
      ],
      speech: events,
    }),
  );
} catch (error) {
  if (page) console.log('Visible error:', await page.getByRole('alert').allTextContents());
  if (page) console.log('Visible buttons:', await page.getByRole('button').allTextContents());
  if (page) console.log('Page text:', (await page.locator('body').innerText()).slice(0, 1200));
  throw error;
} finally {
  await app.close();
}
