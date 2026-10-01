import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { electronTestFlags } from './electron-test-flags.mjs';

// Catalog/UI checks only: no lesson, paid inference, microphone recording or photos.
const app = await electron.launch({ args: ['.', '--test-mode', ...electronTestFlags,
  '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'], cwd: process.cwd() });
let page, saved;
try {
  page = await app.firstWindow();
  page.setDefaultTimeout(20000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.getByRole('button', { name: '家長設定' }).waitFor();
  saved = await page.evaluate(() => Object.fromEntries(Object.entries(localStorage)
    .filter(([key]) => key.startsWith('gpt-live-config:'))));
  await page.evaluate(() => {
    for (const key of Object.keys(localStorage))
      if (key.startsWith('gpt-live-config:')) localStorage.removeItem(key);
  });
  const openGpt = async () => {
    await page.getByRole('button', { name: '家長設定' }).waitFor();
    await page.locator('.advanced > summary').click();
    await page.getByRole('button', { name: 'GPT Live', exact: false }).click();
  };
  const selected = async (label, value) => {
    await page.waitForFunction(({ label, value }) => [...document.querySelectorAll('label')]
      .some(element => element.textContent.includes(label) && element.querySelector('select')?.value === value),
    { label, value });
    assert.equal(await page.getByLabel(label).inputValue(), value);
  };
  await page.reload();
  const response = page.waitForResponse(r => new URL(r.url()).pathname === '/gpt-options');
  await openGpt();
  const catalog = await (await response).json();
  await selected('教學模型', 'gpt-6.1-sol');
  await selected('圖片模型', 'gpt-6-luna');
  await page.getByLabel('教學委派方式').selectOption('client');
  await selected('教學模型', 'gpt-6.1-sol');
  await page.getByLabel('圖片辨識服務').selectOption('codex');
  await selected('圖片模型', 'gpt-6-luna');
  await page.getByLabel('教學服務').selectOption('agy');
  await selected('教學模型', 'gemini-3.8-flash-medium');
  await page.getByLabel('圖片辨識服務').selectOption('agy');
  await selected('圖片模型', 'gemini-3.8-flash-medium');
  // A user may still select any other available model and keep it after reload.
  await page.getByLabel('教學服務').selectOption('codex');
  await page.getByLabel('教學模型').selectOption('gpt-6-astra');
  await page.getByLabel('圖片辨識服務').selectOption('codex');
  await page.getByLabel('圖片模型').selectOption('gpt-6.1-sol');
  await page.reload();
  await openGpt();
  await selected('教學模型', 'gpt-6-astra');
  await selected('圖片模型', 'gpt-6.1-sol');
  await page.locator('.students > button').nth(1).click();
  await selected('教學模型', 'gpt-6.1-sol');
  await selected('圖片模型', 'gpt-6-luna');
  await page.locator('.students > button').first().click();
  await selected('教學模型', 'gpt-6-astra');
  await selected('圖片模型', 'gpt-6.1-sol');
  await page.evaluate(() => localStorage.setItem('gpt-live-config:student-2', JSON.stringify({
    version: 1,
    delegation: { type: 'client', provider: 'codex', model: 'gpt-6-astra', effort: 'low' },
    vision: { provider: 'codex', model: 'gpt-6-sol', effort: 'max' },
  })));
  await page.reload();
  await openGpt();
  await selected('教學模型', 'gpt-6.1-sol');
  await selected('圖片模型', 'gpt-6-luna');
  await selected('教學 effort', 'low');
  await selected('圖片 effort', 'max');

  // A future, deliberately shuffled catalog exercises version ranking and the
  // early-provider-switch case before model discovery has finished.
  const option = id => ({ id, label: id, image: true, efforts: ['low', 'medium', 'max'] });
  const future = {
    openai: { models: ['gpt-6.9-sol', 'gpt-6-luna', 'gpt-6.10-sol', 'gpt-6.2-luna'].map(option), error: null },
    codex: { models: ['gpt-6-astra', 'gpt-6.9-sol', 'gpt-6.2-luna', 'gpt-6.10-sol'].map(option), error: null },
    agy: { models: ['gemini-4-pro-high', 'gemini-3.9-flash-high', 'gemini-3.10-flash-medium'].map(option), error: null },
  };
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route('**/gpt-options', async route => {
    await gate;
    await route.fulfill({ json: future });
  });
  await page.evaluate(() => {
    for (const key of Object.keys(localStorage))
      if (key.startsWith('gpt-live-config:')) localStorage.removeItem(key);
  });
  await page.reload();
  await openGpt();
  await page.getByLabel('教學委派方式').selectOption('client');
  await page.getByLabel('圖片辨識服務').selectOption('codex');
  release();
  await selected('教學模型', 'gpt-6.10-sol');
  await selected('圖片模型', 'gpt-6.2-luna');
  await page.getByLabel('教學服務').selectOption('agy');
  await page.getByLabel('圖片辨識服務').selectOption('agy');
  await selected('教學模型', 'gemini-3.10-flash-medium');
  await selected('圖片模型', 'gemini-3.10-flash-medium');
  await page.getByLabel('教學委派方式').selectOption('responses');
  await page.getByLabel('圖片辨識服務').selectOption('openai');
  await selected('教學模型', 'gpt-6.10-sol');
  await selected('圖片模型', 'gpt-6.2-luna');
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ actual_catalog: {
    codex: catalog.codex.models.map(item => item.id),
    agy_flash: catalog.agy.models.filter(item => item.id.includes('flash')).map(item => item.id),
  }, defaults: 'Sol 6.1 / Luna 6 / Flash 3.8 medium',
  manual_selection_reload_and_student_switch: 'passed', legacy_migration: 'passed',
  future_catalog_and_delayed_discovery: 'passed' }));
} finally {
  if (page && saved && !page.isClosed()) await page.evaluate(saved => {
    for (const key of Object.keys(localStorage))
      if (key.startsWith('gpt-live-config:')) localStorage.removeItem(key);
    for (const [key, value] of Object.entries(saved)) localStorage.setItem(key, value);
  }, saved);
  await app.close();
}
