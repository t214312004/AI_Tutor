// Compare a precisely specified rotation/crop of the same authorized JPEG.
// Does not modify production calibration or the original photo.
import { chromium } from 'playwright';
import fs from 'node:fs/promises';
const [input, output, rotationText, rectangle] = process.argv.slice(2);
const rotation = Number(rotationText);
const crop = rectangle?.split(',').map(Number);
if (!input || !output || ![0, 90, 180, 270].includes(rotation) ||
    (crop && (crop.length !== 4 || crop.some(n => !Number.isFinite(n) || n < 0)))) {
  throw Error('Usage: node scripts/prepare-camera-probe.mjs input.jpg output.jpg rotation [x,y,width,height]');
}
const data = 'data:image/jpeg;base64,' + (await fs.readFile(input)).toString('base64');
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage();
  const result = await page.evaluate(async ({ data, rotation, crop }) => {
    const image = new Image(); image.src = data; await image.decode();
    const canvas = document.createElement('canvas');
    canvas.width = rotation % 180 ? image.height : image.width;
    canvas.height = rotation % 180 ? image.width : image.height;
    const ctx = canvas.getContext('2d');
    ctx.translate(canvas.width / 2, canvas.height / 2); ctx.rotate(rotation * Math.PI / 180);
    ctx.drawImage(image, -image.width / 2, -image.height / 2);
    const target = document.createElement('canvas');
    const [x, y, width, height] = crop ?? [0, 0, canvas.width, canvas.height];
    if (!width || !height || x + width > canvas.width || y + height > canvas.height) throw Error('Crop outside photo');
    target.width = width; target.height = height;
    target.getContext('2d').drawImage(canvas, x, y, width, height, 0, 0, width, height);
    return target.toDataURL('image/jpeg', .95);
  }, { data, rotation, crop });
  await fs.writeFile(output, Buffer.from(result.split(',')[1], 'base64'));
  console.log(JSON.stringify({ input, output, rotation, crop: crop ?? 'whole frame' }));
} finally { await browser.close(); }
