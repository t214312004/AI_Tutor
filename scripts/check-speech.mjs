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
  await page.locator('.preflight-optional > summary').click();
  await page.getByRole('button', { name: '試聽老師聲音' }).click();
  await page.getByRole('button', { name: '停止試聽' }).waitFor({ timeout: 50000 });
  await page.waitForTimeout(650);
  await page.getByRole('button', { name: '停止試聽' }).click();
  assert.equal(await page.getByRole('button', { name: '停止試聽' }).count(), 0);
  await page.getByText('已停止試聽').waitFor();
  console.log(
    JSON.stringify({
      passed: [
        'Edge TTS online synthesis',
        'real playback start',
        'click stops preflight playback',
      ],
    }),
  );
} finally {
  await app.close();
}
