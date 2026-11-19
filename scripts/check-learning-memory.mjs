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
root=Path('.local/electron-test/data')
root.mkdir(parents=True,exist_ok=True)
s=Store(root)
with s.student('student-4') as db:
  db.execute('DELETE FROM learning_attempt_edits')
  db.execute('DELETE FROM learning_attempts')
  db.execute('DELETE FROM practice_settings')
  db.execute('DELETE FROM learning_edits')
  db.execute('DELETE FROM learning_sources')
  db.execute('DELETE FROM learning_evidence')
  db.execute('DELETE FROM learning_overrides')
  db.execute('DELETE FROM events')
  db.execute('DELETE FROM session_reviews')
  db.execute('DELETE FROM session_finalizations')
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
  const practice = page.getByRole('region', { name: '練習與獨立作答' });
  await practice.getByRole('checkbox', { name: '允許老師邀請短檢查' }).check();
  await practice.getByText('設定已儲存', { exact: true }).waitFor();
  await practice.getByText('核對教學示例', { exact: false }).click();
  const card = practice.locator('.teaching-card').filter({ hasText: '同一整體、同分母' });
  await card.getByRole('checkbox').check();
  await practice.getByText('核對教學示例（1 張已確認）').waitFor();
  await practice.getByText('補記一次作答', { exact: true }).click();
  const form = practice.locator('form');
  await form.getByLabel('概念', { exact: true }).fill('同分母分數');
  await form.getByLabel('題目', { exact: true }).fill('同樣大小的整體，4/7和6/7哪個大？');
  await form.getByLabel('作答原文', { exact: true }).fill('6/7，因為每份一樣大，取六份比較多。');
  await form.getByLabel('檢查階段').selectOption('immediate_check');
  await form.getByLabel('核對依據').fill('家長核對等分圖，孩子沒有看解答。');
  await form.getByRole('button', { name: '保存待核對作答' }).click();
  const attempt = practice.locator('.practice-attempt').first();
  await attempt.locator('.practice-attempt-heading span', { hasText: '待核對' }).waitFor();
  await attempt.getByText('核對、修正或排除').click();
  await attempt.getByLabel('家長核對結果').selectOption('correct');
  await attempt.getByRole('button', { name: '儲存練習複核' }).click();
  await practice.getByText('家長已核對本次無即時提示的正確作答', { exact: false }).waitFor();
  await practice.getByText('核對教學示例', { exact: false }).click();
  await practice.locator('h3').scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'test-results/learning-practice-reviewed.png', fullPage: true });
  await practice.locator('.practice-attempt').getByText('核對、修正或排除').click();
  page.once('dialog', (dialog) => void dialog.accept());
  await practice.getByRole('button', { name: '排除練習紀錄' }).click();
  await practice.getByText('尚無練習紀錄；這不表示孩子不會。').waitFor();
  await practice.locator('h3').scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'test-results/learning-practice-parent.png', fullPage: true });
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
        'practice opt-in and card review',
        'independent answer parent review',
        'practice exclusion',
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
root=Path('.local/electron-test/data')
root.mkdir(parents=True,exist_ok=True)
s=Store(root)
with s.student('student-4') as db:
  db.execute('DELETE FROM learning_attempt_edits')
  db.execute('DELETE FROM learning_attempts')
  db.execute('DELETE FROM practice_settings')
  db.execute('DELETE FROM learning_edits')
  db.execute('DELETE FROM learning_sources')
  db.execute('DELETE FROM learning_evidence')
  db.execute('DELETE FROM learning_overrides')
  db.execute('DELETE FROM events')
  db.execute('DELETE FROM session_reviews')
  db.execute('DELETE FROM session_finalizations')
  db.execute('DELETE FROM sessions')
`,
    ],
    { cwd: process.cwd(), stdio: 'pipe' },
  );
}
