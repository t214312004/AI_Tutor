// Isolated synthetic student data; exercises the real parent UI and local API.
import { _electron as electron } from 'playwright';
import { execFileSync } from 'node:child_process';
import assert from 'node:assert/strict';
import { electronTestFlags } from './electron-test-flags.mjs';

execFileSync(
  '.venv/Scripts/python.exe',
  [
    '-c',
    `
from pathlib import Path
from server.test_support import SeededStore as Store
s=Store(Path('.local/electron-test/data'))
with s.student('student-4') as db:
  db.execute('DELETE FROM learning_edits')
  db.execute('DELETE FROM learning_evidence')
  db.execute('DELETE FROM learning_overrides')
  db.execute('DELETE FROM events')
  db.execute('DELETE FROM sessions')
sid=s.start('student-4','basic','合成測試')
s.record_learning_evidence('student-4',sid,{'code':'N-4-5','subject':'數學','label':'同分母分數',
  'kind':'asked_for_help','quote':'合成測試：分數怎麼比較？','origin':'student_utterance','source_page':29})
s.finish('student-4',sid)
`,
  ],
  { cwd: process.cwd(), stdio: 'pipe' },
);

const app = await electron.launch({
  args: ['.', '--test-mode', ...electronTestFlags],
  cwd: process.cwd(),
});
try {
  const page = await app.firstWindow();
  await page.getByRole('button', { name: '家長設定' }).waitFor();
  await page.locator('.students .student').nth(1).click();
  await page.getByRole('button', { name: '家長設定' }).click();
  await page.getByRole('tab', { name: '學習紀錄', exact: true }).click();
  await page.getByText('長期教學記憶').waitFor();
  await page.locator('.memory-card').first().getByText('同分母分數').waitFor();
  await page.locator('.memory-card select').first().selectOption('needs_review');
  await page.getByPlaceholder('例如：先用實物示範，再畫圖。').fill('先用圖解');
  await page.getByRole('button', { name: '儲存教學註記' }).click();
  await page.getByText('查看與修正原始證據').click();
  const quote = page.getByRole('textbox', { name: '修正證據內容' }).first();
  await quote.fill('家長修正：比較同分母分數時想先畫圖');
  await page.getByRole('button', { name: '儲存修正' }).click();
  assert.equal(
    await page.getByRole('textbox', { name: '修正證據內容' }).first().inputValue(),
    '家長修正：比較同分母分數時想先畫圖',
  );
  page.once('dialog', (dialog) => void dialog.accept());
  await page.getByRole('button', { name: '排除這筆' }).click();
  const records = await page.evaluate(async () => {
    const connection = await window.desktop.connection();
    const read = async (id) =>
      (
        await fetch(connection.base + `/students/${id}/history`, {
          headers: { Authorization: 'Bearer ' + connection.token },
        })
      ).json();
    return { target: await read('student-4'), other: await read('student-2') };
  });
  assert.equal(records.target.learning_evidence.length, 0);
  assert.equal(
    records.target.learning_profile.find((item) => item.code === 'N-4-5').parent_note,
    '先用圖解',
  );
  assert.equal(
    records.other.learning_profile.some((item) => item.code === 'N-4-5'),
    false,
  );
  console.log(
    JSON.stringify({
      passed: [
        'parent profile',
        'note saved',
        'evidence corrected',
        'evidence excluded',
        'student isolation',
      ],
    }),
  );
} finally {
  await app.close();
  execFileSync(
    '.venv/Scripts/python.exe',
    [
      '-c',
      `
from pathlib import Path
from server.test_support import SeededStore as Store
s=Store(Path('.local/electron-test/data'))
with s.student('student-4') as db:
  db.execute('DELETE FROM learning_edits')
  db.execute('DELETE FROM learning_evidence')
  db.execute('DELETE FROM learning_overrides')
  db.execute('DELETE FROM events')
  db.execute('DELETE FROM sessions')
`,
    ],
    { cwd: process.cwd(), stdio: 'pipe' },
  );
}
