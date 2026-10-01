const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { resolveStorage } = require('../electron/storage-paths.cjs');

function withRoot(fn) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tutor-paths-'));
  try { fn(root); } finally { fs.rmSync(root, { recursive: true, force: true }); }
}

test('fresh install uses public default; explicit home stays where requested', () => withRoot(root => {
  const base = path.join(root, 'public-default');
  assert.equal(resolveStorage(root, { LOCALAPPDATA: base }).home, path.join(base, 'AI-Tutor'));
  const home = path.join(root, 'private-data');
  assert.equal(resolveStorage(root, { TUTOR_HOME: home }).home, home);
}));

test('local config keeps all family locations; tests ignore it', () => withRoot(root => {
  const home = path.join(root, 'app-data');
  const curriculum = path.join(root, 'data');
  fs.mkdirSync(home);
  fs.writeFileSync(path.join(home, 'local-settings.json'), JSON.stringify({ home, curriculum_dir: curriculum }));
  assert.deepEqual(resolveStorage(root, {}), { home, data: path.join(home, 'data'), curriculum });
  const fixture = resolveStorage(root, { TUTOR_HOME: home }, true);
  assert.equal(fixture.home, path.join(root, '.local/electron-test'));
  assert.notEqual(fixture.data, path.join(home, 'data'));
}));

test('invalid explicit local config fails instead of switching to default', () => withRoot(root => {
  fs.mkdirSync(path.join(root, 'app-data'));
  fs.writeFileSync(path.join(root, 'app-data/local-settings.json'), JSON.stringify({ home: path.join(root, 'missing') }));
  assert.throws(() => resolveStorage(root, {}), /本機資料位置/);
  assert.throws(() => resolveStorage(root, { TUTOR_HOME: 'relative' }), /絕對路徑/);
}));
