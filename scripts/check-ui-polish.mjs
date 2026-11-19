// Visual and keyboard regression coverage, using an isolated synthetic household.
import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { electronTestFlags } from './electron-test-flags.mjs';

await fs.mkdir('.local', { recursive: true });
const home = await fs.mkdtemp(path.resolve('.local/ui-polish-'));
const output = path.resolve('test-results/ui-polish');
await fs.mkdir(output, { recursive: true });
const app = await electron.launch({
  args: ['.', '--test-mode', ...electronTestFlags, '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'],
  cwd: process.cwd(), env: { ...process.env, TUTOR_TEST_HOME: home },
});
const checks = [];
try {
  const page = await app.firstWindow();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.getByRole('button', { name: '檢查訂閱並開始陪讀' }).waitFor();
  await page.getByRole('button', { name: '關閉鏡頭', exact: true }).click();
  for (const [width, height] of [[1440, 900], [1280, 720], [911, 512], [390, 844]]) {
    await page.setViewportSize({ width, height });
    await page.evaluate(() => window.scrollTo(0, 0));
    const metrics = await page.evaluate(() => ({
      width: innerWidth, scroll: document.documentElement.scrollWidth,
      start: document.querySelector('.start').getBoundingClientRect().bottom,
      height: innerHeight,
    }));
    assert.ok(metrics.scroll <= metrics.width, JSON.stringify(metrics));
    assert.ok(metrics.start <= metrics.height, JSON.stringify(metrics));
    const preview = await page.locator('.preflight-frame').boundingBox();
    assert.ok(preview.width > 200 && preview.height > 120, 'Disabled camera retains a readable empty state');
    await page.screenshot({ path: path.join(output, `home-${width}.png`), animations: 'disabled' });
    await page.getByRole('button', { name: '家長設定', exact: true }).click();
    for (const name of ['個人檔案', '學習紀錄', '學校與課綱']) {
      await page.getByRole('tab', { name, exact: true }).click();
      const layout = await page.getByRole('dialog').evaluate(element => ({
        right: element.getBoundingClientRect().right,
        bottom: element.getBoundingClientRect().bottom,
        width: innerWidth, height: innerHeight,
        contentWidth: element.querySelector('.modal-content').clientWidth,
        contentScroll: element.querySelector('.modal-content').scrollWidth,
      }));
      assert.ok(layout.right <= layout.width && layout.bottom <= layout.height, JSON.stringify(layout));
      assert.ok(layout.contentScroll <= layout.contentWidth + 1, JSON.stringify(layout));
      await page.screenshot({ path: path.join(output, `settings-${name}-${width}.png`), animations: 'disabled' });
    }
    await page.keyboard.press('Escape');
    checks.push(`home and all settings at ${width}×${height}`);
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole('button', { name: '家長設定', exact: true }).click();
  await page.getByRole('tab', { name: '個人檔案', exact: true }).click();
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.getByRole('tab', { name: '學習紀錄', exact: true }).evaluate(el => el === document.activeElement), true);
  await page.keyboard.press('Enter');
  assert.equal(await page.getByRole('tab', { name: '學習紀錄', exact: true }).getAttribute('aria-selected'), 'true');
  await page.keyboard.press('End');
  await page.keyboard.press('Enter');
  assert.equal(await page.getByRole('tab', { name: '學校與課綱', exact: true }).getAttribute('aria-selected'), 'true');
  await page.keyboard.press('Home');
  await page.keyboard.press('Enter');
  assert.equal(await page.getByRole('tab', { name: '個人檔案', exact: true }).getAttribute('aria-selected'), 'true');
  await page.keyboard.press('Escape');
  await page.getByRole('dialog', { name: '家長設定', exact: true }).waitFor({ state: 'hidden' });
  await page.getByRole('button', { name: '家長設定', exact: true }).and(page.locator(':focus')).waitFor();
  checks.push('keyboard tabs, activation, Escape, and focus restoration');

  await page.getByRole('button', { name: '檢查訂閱並開始陪讀' }).click();
  await page.getByRole('button', { name: '結束', exact: true }).waitFor();
  await page.evaluate(() => window.desktop.focus(false));
  for (const [width, height] of [[1440, 900], [911, 512], [390, 844]]) {
    await page.setViewportSize({ width, height });
    const metrics = await page.evaluate(() => ({
      width: innerWidth, scroll: document.documentElement.scrollWidth,
      height: innerHeight, scrollHeight: document.documentElement.scrollHeight,
      controls: document.querySelector('.controls').getBoundingClientRect().bottom,
    }));
    assert.ok(metrics.scroll <= width && metrics.scrollHeight <= height && metrics.controls <= height, JSON.stringify(metrics));
    await page.screenshot({ path: path.join(output, `lesson-${width}.png`) });
    checks.push(`lesson controls fit ${width}×${height}`);
  }
  await page.emulateMedia({ reducedMotion: 'reduce' });
  assert.equal(await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches), true);
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.route('**/sessions/*/finalize', route => route.fulfill({
    json: { session_id: 'visual-fixture', state: 'failed', stage: 'summarizing', snapshot_saved: true, summary_available: false, online: false },
  }));
  await page.getByRole('button', { name: '結束', exact: true }).click();
  await page.getByRole('button', { name: '重試整理', exact: true }).waitFor();
  await page.screenshot({ path: path.join(output, 'finalization-recovery.png') });
  checks.push('finalization recovery screen');
  await page.evaluate(() => window.desktop.setFinalizing(false));
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: checks, screenshots: output, realServicesUsed: false }, null, 2));
} finally {
  await app.close();
  await fs.rm(home, { recursive: true, force: true }).catch(() => {});
}
