import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { electronTestFlags } from './electron-test-flags.mjs';

const app = await electron.launch({
  args: ['.', '--test-mode', ...electronTestFlags, '--use-fake-ui-for-media-stream'],
  cwd: process.cwd(),
});
try {
  const page = await app.firstWindow();
  page.setDefaultTimeout(15000);
  await page.getByRole('button', { name: '關閉鏡頭' }).waitFor();
  await page.getByRole('button', { name: '關閉鏡頭' }).click();
  await page.evaluate(async () => {
    localStorage.removeItem('camera-settings-v2');
    localStorage.removeItem('camera-device-id');
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    const pattern = document.createElement('canvas');
    pattern.width = 640;
    pattern.height = 360;
    const ctx = pattern.getContext('2d');
    const paint = () => {
      for (const [color, x, y] of [
        ['#f00000', 0, 0],
        ['#00e000', 320, 0],
        ['#0000e0', 0, 180],
        ['#e0d000', 320, 180],
      ]) {
        ctx.fillStyle = color;
        ctx.fillRect(x, y, 320, 180);
      }
    };
    paint();
    await window.desktop.setTestPhoto(pattern.toDataURL('image/png'));
    setInterval(paint, 100);
    const stream = pattern.captureStream(10);
    navigator.mediaDevices.getUserMedia = (constraints) =>
      constraints.video ? Promise.resolve(stream.clone()) : original(constraints);
    const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function (...args) {
      const result = originalToDataURL.apply(this, args);
      if (args[0] === 'image/jpeg') window.__lastCapture = result;
      return result;
    };
  });
  await page.getByRole('button', { name: '開啟鏡頭' }).click();
  await page.getByRole('button', { name: '關閉鏡頭' }).waitFor();
  await page.waitForFunction(() => document.querySelector('.preflight-frame canvas')?.width > 0);
  await page.getByRole('button', { name: '調整拍攝範圍' }).click();
  await page.evaluate(() => window.scrollTo(0, 0));
  const frame = await page.locator('.preflight-frame').boundingBox();
  const targets = [
    [0.1, 0.1],
    [0.9, 0.08],
    [0.85, 0.9],
    [0.15, 0.92],
  ];
  for (let index = 0; index < 4; index++) {
    const handle = await page
      .getByRole('button', { name: `拍攝範圍第 ${index + 1} 角` })
      .boundingBox();
    await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2);
    await page.mouse.down();
    await page.mouse.move(
      frame.x + frame.width * targets[index][0],
      frame.y + frame.height * targets[index][1],
      { steps: 5 },
    );
    await page.mouse.up();
  }
  await page.getByRole('button', { name: '套用範圍' }).click();
  await page.waitForTimeout(700);
  const sample = async (selector) =>
    page.locator(selector).evaluate((canvas) => {
      const ctx = canvas.getContext('2d');
      return [
        [0.25, 0.25],
        [0.75, 0.25],
        [0.25, 0.75],
        [0.75, 0.75],
      ].map(([u, v]) =>
        [
          ...ctx.getImageData(Math.floor(u * canvas.width), Math.floor(v * canvas.height), 1, 1)
            .data,
        ].slice(0, 3),
      );
    });
  const before = await sample('.preflight-frame canvas');
  await page.locator('button.start').click();
  await page.getByRole('button', { name: '關鏡頭', exact: true }).waitFor();
  await page.waitForFunction(() => window.__lastCapture, { timeout: 15000 });
  await page.waitForTimeout(600);
  const lesson = await sample('.camera-view canvas');
  const captured = await page.evaluate(async () => {
    const image = new Image();
    image.src = window.__lastCapture;
    await image.decode();
    const canvas = document.createElement('canvas');
    canvas.width = image.width;
    canvas.height = image.height;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(image, 0, 0);
    return [
      [0.25, 0.25],
      [0.75, 0.25],
      [0.25, 0.75],
      [0.75, 0.75],
    ].map(([u, v]) =>
      [
        ...ctx.getImageData(Math.floor(u * canvas.width), Math.floor(v * canvas.height), 1, 1).data,
      ].slice(0, 3),
    );
  });
  for (let i = 0; i < 4; i++)
    for (let channel = 0; channel < 3; channel++) {
      assert.ok(
        Math.abs(before[i][channel] - lesson[i][channel]) <= 12,
        JSON.stringify({ before, lesson }),
      );
      assert.ok(
        Math.abs(before[i][channel] - captured[i][channel]) <= 20,
        JSON.stringify({ before, captured }),
      );
    }
  const beforeZoom = await page.evaluate(() => window.__lastCapture);
  await page.getByRole('button', { name: '放大這題' }).click();
  const zoomBox = await page.locator('.camera-view canvas').boundingBox();
  await page.mouse.move(zoomBox.x + zoomBox.width * 0.2, zoomBox.y + zoomBox.height * 0.2);
  await page.mouse.down();
  await page.mouse.move(zoomBox.x + zoomBox.width * 0.8, zoomBox.y + zoomBox.height * 0.8,
    { steps: 6 });
  await page.mouse.up();
  await page.getByText('已放大題目').waitFor();
  await page.waitForFunction((previous) => window.__lastCapture !== previous, beforeZoom);
  const zoomedPreview = await sample('.camera-view canvas');
  const zoomedCapture = await page.evaluate(async () => {
    const image = new Image(); image.src = window.__lastCapture; await image.decode();
    const canvas = document.createElement('canvas');
    canvas.width = image.width; canvas.height = image.height;
    const ctx = canvas.getContext('2d'); ctx.drawImage(image, 0, 0);
    return { width:image.width, pixels:[[0.25,0.25],[0.75,0.25],[0.25,0.75],[0.75,0.75]]
      .map(([u,v]) => [...ctx.getImageData(Math.floor(u*canvas.width),Math.floor(v*canvas.height),1,1).data].slice(0,3)) };
  });
  for (let i = 0; i < 4; i++)
    for (let channel = 0; channel < 3; channel++)
      assert.ok(Math.abs(zoomedPreview[i][channel]-zoomedCapture.pixels[i][channel]) <= 20,
        JSON.stringify({ zoomedPreview, zoomedCapture }));
  assert.ok(zoomedCapture.width > 0);
  await page.getByRole('button', { name: '結束', exact: true }).click();
  await page.getByRole('button', { name: '開始專注工作區' }).waitFor({ timeout: 15000 });
  console.log(
    JSON.stringify({
      passed: [
        'calibrated preflight equals lesson preview',
        'calibrated preflight equals submitted JPEG',
        'focused question preview equals submitted JPEG',
      ],
    }),
  );
} finally {
  await app.close();
}
