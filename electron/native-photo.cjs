const { execFile } = require('node:child_process');
const path = require('node:path');

function photoPacket(value) {
  if (value.error) throw Error(`Windows 拍照失敗：${String(value.error).slice(0, 300)}`);
  if (typeof value.image !== 'string' || !value.image.startsWith('data:image/png;base64,') || value.image.length > 16_000_000)
    throw Error('原生照片格式或大小不符合上傳限制');
  const bytes = Buffer.from(value.image.slice(22), 'base64');
  if (bytes.length < 24 || bytes.subarray(0, 8).toString('hex') !== '89504e470d0a1a0a')
    throw Error('原生照片不是有效的 PNG');
  return { image: value.image, width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20),
    method: value.method, captured_at: value.captured_at };
}

function createNativePhoto({ testing = false, run = execFile } = {}) {
  let active = null;
  let fixture = null;
  return {
    setFixture(image) {
      if (!testing) throw Error('Photo fixtures are only available in test mode');
      fixture = photoPacket({ image, method: 'test-fixture', captured_at: new Date().toISOString() });
    },
    cancel() { active?.kill(); },
    async take({ label, aspectRatio }) {
      if (active) throw Error('相機正在拍照');
      if (testing) {
        if (!fixture) throw Error('測試模式必須提供合成照片');
        return { ...fixture, captured_at: new Date().toISOString() };
      }
      if (process.platform !== 'win32') throw Error('原生拍照目前需要 Windows');
      if (typeof label !== 'string' || !label || label.length > 300 ||
          !Number.isFinite(aspectRatio) || aspectRatio < 0.1 || aspectRatio > 10)
        throw Error('無效的原生拍照裝置資訊');
      return new Promise((resolve, reject) => {
        const executable = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
        active = run(executable, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
          '-File', path.join(__dirname, 'native-photo.ps1'), '-CameraLabel', label,
          '-AspectRatio', String(aspectRatio)], { windowsHide: true, timeout: 20_000, maxBuffer: 20_000_000, encoding: 'utf8' }, (error, stdout) => {
          active = null;
          if (error) { reject(Error(error.killed ? '原生拍照已取消或逾時' : '無法啟動 Windows 原生拍照')); return; }
          try { resolve(photoPacket(JSON.parse(stdout.replace(/^\uFEFF/, '').trim()))); }
          catch (failure) { reject(failure); }
        });
      });
    },
  };
}
module.exports = { createNativePhoto };
