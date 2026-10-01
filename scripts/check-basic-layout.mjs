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
  await page.getByRole('button', { name: '家長設定' }).waitFor();
  await page.evaluate(async () => {
    const c = await window.desktop.connection();
    await fetch(c.base + '/credentials', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + c.token, 'Content-Type': 'application/json' },
      body: JSON.stringify({ GROQ_API_KEY: 'test-only' }),
    });
  });
  await page.reload();
  await page.getByRole('button', { name: '檢查並開始陪讀', exact: true }).waitFor();
  await page.route('**/sessions', (route) =>
    route.fulfill({ json: { id: 'layout-fixture', online: true } }),
  );
  await page.route('**/sessions/layout-fixture/**', (route) => route.fulfill({ json: {} }));
  await page.route('**/sessions/layout-fixture/finalize', (route) => route.fulfill({ json: {
    session_id: 'layout-fixture', state: 'completed', stage: 'completed',
    snapshot_saved: true, summary_available: false, online: true,
  } }));
  await page.evaluate(() => {
    window.WebSocket = class {
      static OPEN = 1;
      readyState = 0;
      bufferedAmount = 0;
      constructor(url) {
        this.id = url.split('/').at(-2);
        setTimeout(() => {
          this.readyState = 1;
          this.onopen?.();
        }, 0);
      }
      emit(data) {
        this.onmessage?.({ data: JSON.stringify(data) });
      }
      send(raw) {
        const m = JSON.parse(raw);
        if (m.type === 'authenticate')
          this.emit({ type: 'ready', session_id: this.id, generation: 0 });
        if (m.type === 'begin') {
          this.emit({
            type: 'lesson',
            confirmed: true,
            all_done: false,
            tasks: [
              { id: 'math', title: '數學習作第12頁：練習加法，逐題核對作答', status: 'todo' },
            ],
          });
          for (let i = 0; i < 8; i++)
            this.emit({
              type: 'transcript',
              role: i % 2 ? 'assistant' : 'user',
              text:
                i % 2 ? '先看看題目告訴你哪些數字，試著畫一個圖。' : '伴讀老師，這一題我想試試看。',
            });
        }
        if (m.type === 'close') this.emit({ type: 'closed' });
      }
      close() {
        this.readyState = 3;
        this.onclose?.();
      }
    };
  });
  await page.getByRole('button', { name: '檢查並開始陪讀', exact: true }).click();
  await page.locator('.lesson-list input').waitFor();
  await page.evaluate(() => window.desktop.focus(false));
  await app.evaluate(async ({ BrowserWindow }) => {
    const main = BrowserWindow.getAllWindows().find((w) =>
      w.webContents.getURL().startsWith('file:'),
    );
    if (main.isFullScreen())
      await new Promise((resolve) => main.once('leave-full-screen', resolve));
    main.unmaximize();
    main.setSize(1100, 760);
  });
  await page.waitForFunction(() => innerWidth < 1200);
  const layout = await page.evaluate(() => ({
    width: innerWidth,
    height: innerHeight,
    controls: [...document.querySelectorAll('.controls button')].map(
      (e) => e.getBoundingClientRect().bottom,
    ),
    checkbox: document.querySelector('.lesson-list input').getBoundingClientRect().width,
    chat: document.querySelector('.messages').getBoundingClientRect().height,
  }));
  assert.ok(layout.controls.every((y) => y <= layout.height));
  assert.ok(layout.checkbox <= 20);
  assert.ok(layout.chat >= 60);
  await page.screenshot({ path: 'test-results/basic-layout-compact.png' });
  await page.getByRole('button', { name: '結束', exact: true }).click();
  await page.getByRole('button', { name: '檢查並開始陪讀', exact: true }).waitFor();
  console.log(
    JSON.stringify({
      passed: [
        'compact window',
        'task checkbox sizing',
        'scrollable conversation',
        'all three controls visible',
      ],
      layout,
    }),
  );
} finally {
  await app.close();
}
