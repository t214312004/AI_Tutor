import { chromium } from 'playwright';
import fs from 'node:fs/promises';
await fs.mkdir('test-results', { recursive: true });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 800, height: 600 } });
  await page.setContent(
    '<html><body style="margin:60px;background:white;font-family:Arial;color:#111"><p style="font-size:22px">SYNTHETIC TEST · NO STUDENT DATA</p><h1 style="font-size:86px">7 + 5 = 12</h1><p style="font-size:24px">Camera/vision adapter fixture</p></body></html>',
  );
  await page.screenshot({ path: 'test-results/synthetic-worksheet.png' });
  await page.screenshot({ path: 'test-results/synthetic-worksheet.jpg', type: 'jpeg', quality: 95 });
} finally {
  await browser.close();
}
