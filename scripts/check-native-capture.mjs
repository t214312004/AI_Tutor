// Synthetic 1080p sensor pattern: verify the actual production capture module.
import { chromium } from 'playwright';
import { build } from 'esbuild';
import fs from 'node:fs/promises';
import assert from 'node:assert/strict';

const bundle = await build({ entryPoints: ['src/camera.ts'], bundle: true, write: false,
  format: 'iife', globalName: 'cameraModule' });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage();
  await page.addScriptTag({ content: bundle.outputFiles[0].text });
  const result = await page.evaluate(async () => {
    const source = document.createElement('canvas'); source.width = 1920; source.height = 1080;
    const ctx = source.getContext('2d');
    const pixels = ctx.createImageData(1920, 1080);
    let seed = 123456;
    for (let i = 0; i < pixels.data.length; i += 4) {
      for (let c = 0; c < 3; c++) {
        seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
        pixels.data[i + c] = seed >>> 24;
      }
      pixels.data[i + 3] = 255;
    }
    ctx.putImageData(pixels, 0, 0);
    const stream = source.captureStream(10);
    const video = document.createElement('video'); video.muted = true; video.srcObject = stream;
    await video.play();
    const mod = cameraModule;
    const settings = { ...mod.defaultCameraSettings, rotation: 0, perspectiveEnabled: false };
    const png = mod.capture(video, settings, 'png');
    const image = new Image(); image.src = png; await image.decode();
    const photoInput = mod.capture(image, settings, 'png');
    const decoded = document.createElement('canvas'); decoded.width = image.width; decoded.height = image.height;
    decoded.getContext('2d').drawImage(image, 0, 0);
    const actual = decoded.getContext('2d').getImageData(0, 0, image.width, image.height).data;
    const reference = mod.renderFrame(video, settings, Infinity, true, true)
      .getContext('2d').getImageData(0, 0, 1920, 1080).data;
    let mismatches = 0;
    for (let i = 0; i < actual.length; i++) if (actual[i] !== reference[i]) mismatches++;
    const sizes = [];
    for (const [name, cfg] of [
      ['whole', settings],
      ['quarter', { ...settings, corners: [[.25,.25],[.75,.25],[.75,.75],[.25,.75]] }],
      ['vertical', { ...settings, corners: [[0,0],[.1,0],[.1,1],[0,1]] }],
    ]) {
      const frame = mod.renderFrame(video, cfg, Infinity, false, true);
      sizes.push({ name, width: frame.width, height: frame.height });
    }
    const perspectiveChecks = [];
    for (const aspect of ['auto', 'a4-portrait']) {
      const cfg = { ...settings, perspectiveEnabled: true, outputAspect: aspect,
        corners: [[.05,.05],[.95,.05],[.7,.95],[.3,.95]] };
      const frame = mod.renderFrame(video, cfg, Infinity, false, true);
      const encoded = mod.capture(video, cfg, 'png');
      const photo = new Image(); photo.src = encoded; await photo.decode();
      const roundtrip = document.createElement('canvas');
      roundtrip.width = photo.width; roundtrip.height = photo.height;
      roundtrip.getContext('2d').drawImage(photo, 0, 0);
      const before = frame.getContext('2d').getImageData(0,0,frame.width,frame.height).data;
      const after = roundtrip.getContext('2d').getImageData(0,0,photo.width,photo.height).data;
      let changed = 0;
      for (let i = 0; i < before.length; i++) if (before[i] !== after[i]) changed++;
      const top = .9 * 1920;
      const side = Math.hypot(.25 * 1920, .9 * 1080);
      perspectiveChecks.push({ aspect, width: photo.width, height: photo.height,
        longest_horizontal: top, longest_vertical: side, pixel_mismatches: changed });
    }
    stream.getTracks().forEach(track => track.stop());
    return { png, photoInput, width: image.width, height: image.height, mismatches, sizes, perspectiveChecks };
  });
  assert.equal(result.width, 1920); assert.equal(result.height, 1080);
  assert.equal(result.mismatches, 0, 'PNG must preserve every pixel from the camera canvas');
  assert.equal(result.photoInput, result.png, 'decoded native photos use the same lossless geometry');
  assert.deepEqual(result.sizes, [
    { name: 'whole', width: 1920, height: 1080 },
    { name: 'quarter', width: 960, height: 540 },
    { name: 'vertical', width: 192, height: 1080 },
  ]);
  for (const check of result.perspectiveChecks) {
    assert.ok(check.width >= Math.round(check.longest_horizontal), 'retain longer horizontal edge');
    assert.ok(check.height >= Math.round(check.longest_vertical), 'retain longer vertical edge');
    assert.equal(check.pixel_mismatches, 0, 'PNG preserves the rectified canvas without further loss');
  }
  await fs.mkdir('test-results', { recursive: true });
  const bytes = Buffer.from(result.png.split(',')[1], 'base64');
  await fs.writeFile('test-results/native-camera-synthetic.png', bytes);
  const report = { width: result.width, height: result.height, pixel_mismatches: result.mismatches,
    sizes: result.sizes, perspective: result.perspectiveChecks,
    png_bytes: bytes.length, frame_characters: result.png.length };
  await fs.writeFile('test-results/native-camera-capture.json', JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} finally { await browser.close(); }
