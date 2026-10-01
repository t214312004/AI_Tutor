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
  await page.getByRole('button', { name: '家長設定' }).waitFor();
  const layouts = [];
  for (const size of [[1366, 768], [910, 620]]) {
    await app.evaluate(({ BrowserWindow }, target) => {
      const main = BrowserWindow.getAllWindows().find(w => w.webContents.getURL().startsWith('file:'));
      main.unmaximize();
      main.setSize(target[0], target[1]);
    }, size);
    await page.waitForFunction(width => Math.abs(innerWidth - width) < 30, size[0]);
    layouts.push(await page.evaluate(() => {
      const rect = selector => {
        const r = document.querySelector(selector).getBoundingClientRect();
        return { top: Math.round(r.top), bottom: Math.round(r.bottom), height: Math.round(r.height) };
      };
      return {
        viewport: { width: innerWidth, height: innerHeight },
        pageHeight: document.documentElement.scrollHeight,
        camera: rect('.preflight-camera'),
        students: rect('.students'),
        start: rect('button.start'),
      };
    }));
    const students = page.locator('.students button');
    for (let index = 0; index < await students.count(); index++) {
      await students.nth(index).click();
      const startBottom = await page.locator('button.start').evaluate(
        element => Math.round(element.getBoundingClientRect().bottom));
      const viewportHeight = await page.evaluate(() => innerHeight);
      assert.ok(startBottom <= viewportHeight,
        `Start action for student ${index + 1} must be visible at ${size.join('x')}`);
    }
    await students.first().click();
  }
  assert.ok(layouts.every(item => item.start.bottom <= item.viewport.height),
    'Start action must remain visible in the initial viewport');
  await page.screenshot({ path: 'test-results/usability-home-compact.png' });
  await page.getByRole('button', { name: '家長設定' }).click();
  await page.waitForFunction(() => !!document.activeElement.closest('[role="dialog"]'));
  const initialFocusInDialog = await page.evaluate(() => !!document.activeElement.closest('[role="dialog"]'));
  await page.getByRole('button', { name: '關閉設定' }).focus();
  await page.keyboard.press('Shift+Tab');
  const reverseTabInDialog = await page.evaluate(() => !!document.activeElement.closest('[role="dialog"]'));
  await page.keyboard.press('Escape');
  const escapeClosesDialog = await page.getByRole('dialog', { name: '家長設定' }).count() === 0;
  assert.ok(initialFocusInDialog && reverseTabInDialog && escapeClosesDialog,
    'Parent dialog must contain keyboard focus and close on Escape');
  await page.getByRole('button', { name: '家長設定' }).click();
  const profileName = page.getByRole('dialog', { name: '家長設定' }).getByRole('textbox', { name: '稱呼' });
  const originalName = await profileName.inputValue();
  await profileName.fill('未儲存測試');
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: '關閉設定' }).click();
  const unsavedNameShownOnHome = await page.locator('.student.selected b').textContent();
  assert.equal(unsavedNameShownOnHome, originalName,
    'Unsaved profile changes must not leak into home screen');
  await page.getByRole('button', { name: '服務設定', exact: true }).click();
  assert.equal(await page.getByRole('tab', { name: '服務設定' }).getAttribute('aria-selected'), 'true');
  await page.getByRole('button', { name: '關閉設定' }).click();
  await page.reload();
  await page.getByRole('button', { name: '開始專注工作區' }).waitFor();
  const nameAfterReload = await page.locator('.student.selected b').textContent();
  assert.equal(nameAfterReload, originalName);
  await page.getByRole('button', { name: '開始專注工作區' }).click();
  await page.getByRole('button', { name: '結束', exact: true }).waitFor();
  await page.evaluate(() => window.desktop.focus(false));
  const sessions = [];
  for (const size of [[1100, 760], [910, 620]]) {
    await app.evaluate(({ BrowserWindow }, target) => {
      const main = BrowserWindow.getAllWindows().find(w => w.webContents.getURL().startsWith('file:'));
      main.unmaximize();
      main.setSize(target[0], target[1]);
    }, size);
    await page.waitForFunction(width => Math.abs(innerWidth - width) < 30, size[0]);
    sessions.push(await page.evaluate(() => {
      const r = selector => document.querySelector(selector).getBoundingClientRect();
      return {
        viewport: { width: innerWidth, height: innerHeight },
        documentHeight: document.documentElement.scrollHeight,
        workspaceBottom: Math.round(r('.workspace').bottom),
        boardHeight: Math.round(r('.board').height),
        conversationHeight: Math.round(r('.conversation').height),
        messagesHeight: Math.round(r('.messages').height),
        controlsBottom: Math.round(r('.controls').bottom),
      };
    }));
  }
  await page.setViewportSize({ width: 911, height: 512 });
  sessions.push(await page.evaluate(() => ({
    viewport: { width: innerWidth, height: innerHeight },
    documentHeight: document.documentElement.scrollHeight,
    workspaceBottom: Math.round(document.querySelector('.workspace').getBoundingClientRect().bottom),
    boardHeight: Math.round(document.querySelector('.board').getBoundingClientRect().height),
    conversationHeight: Math.round(document.querySelector('.conversation').getBoundingClientRect().height),
    messagesHeight: Math.round(document.querySelector('.messages').getBoundingClientRect().height),
    controlsBottom: Math.round(document.querySelector('.controls').getBoundingClientRect().bottom),
    simulatedViewport: true,
  })));
  assert.ok(sessions.every(item => item.controlsBottom <= item.viewport.height &&
    item.documentHeight <= item.viewport.height),
    'Session controls must stay inside the visible viewport');
  await page.screenshot({ path: 'test-results/usability-session-compact.png' });
  await page.getByRole('button', { name: '顯示預覽' }).click();
  const expandedControlsBottom = await page.locator('.controls').evaluate(
    element => Math.round(element.getBoundingClientRect().bottom));
  assert.ok(expandedControlsBottom <= 512, 'Expanded camera preview must retain session controls');
  await page.getByRole('button', { name: '收起預覽' }).click();
  await page.getByRole('button', { name: '結束', exact: true }).click();
  await page.getByRole('button', { name: '開始專注工作區' }).waitFor();
  const shortHomeStartBottom = await page.locator('button.start').evaluate(
    element => Math.round(element.getBoundingClientRect().bottom));
  assert.ok(shortHomeStartBottom <= 512,
    'Start action must remain visible in a short high DPI viewport');
  await page.screenshot({ path: 'test-results/usability-home-short.png' });
  console.log(JSON.stringify({ layouts, initialFocusInDialog, reverseTabInDialog, escapeClosesDialog,
    shortHomeStartBottom, unsavedProfile: { originalName, unsavedNameShownOnHome, nameAfterReload }, sessions,
    expandedControlsBottom }, null, 2));
} finally {
  await app.close();
}
