const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createNativePhoto } = require('../electron/native-photo.cjs');
const image = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aY1sAAAAASUVORK5CYII=';

test('test mode cannot accidentally open hardware', async () => {
  const service = createNativePhoto({ testing: true, run() { throw Error('hardware accessed'); } });
  await assert.rejects(service.take({}), /合成照片/);
  service.setFixture(image);
  const photo = await service.take({});
  assert.equal(photo.image, image);
  assert.equal(photo.width, 1);
  assert.equal(photo.method, 'test-fixture');
});

test('native worker is hidden, serialized and cancellable without returning a stale photo', async () => {
  let done;
  let killed = false;
  const service = createNativePhoto({ run(executable, args, options, callback) {
    assert.equal(options.windowsHide, true);
    assert.ok(executable.endsWith('powershell.exe'));
    assert.equal(args.at(-3), 'Insta360 Link 2C');
    done = callback;
    return { kill() { killed = true; done({ killed: true }); } };
  } });
  const pending = service.take({ label: 'Insta360 Link 2C', aspectRatio: 16 / 9 });
  await assert.rejects(service.take({ label: 'Insta360 Link 2C', aspectRatio: 16 / 9 }), /正在拍照/);
  const rejected = assert.rejects(pending, /取消或逾時/);
  service.cancel();
  await rejected;
  assert.equal(killed, true);
  const next = service.take({ label: 'Insta360 Link 2C', aspectRatio: 16 / 9 });
  done(null, JSON.stringify({ image, method: 'windows-native-photo' }));
  assert.equal((await next).width, 1);
});
