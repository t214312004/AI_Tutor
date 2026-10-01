const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const root = path.resolve(__dirname, '..');
const { resolveStorage } = require('./storage-paths.cjs');
const testing = process.argv.some(arg => ['--smoke', '--device-probe', '--test-mode'].includes(arg));
const locations = resolveStorage(root, process.env, testing);
const shared = locations.home;
function crypto(mode, input) {
  return execFileSync(
    'powershell.exe',
    [
      '-NoProfile',
      '-NonInteractive',
      '-ExecutionPolicy',
      'Bypass',
      '-File',
      path.join(root, 'scripts/machine-crypto.ps1'),
      mode,
    ],
    {
      input,
      encoding: 'utf8',
      windowsHide: true,
      timeout: 10000,
      stdio: ['pipe', 'pipe', 'pipe'],
    },
  );
}
function readKeys(directory = shared) {
  const file = path.join(directory, 'credentials.machine');
  return fs.existsSync(file) ? JSON.parse(crypto('decrypt', fs.readFileSync(file, 'utf8'))) : {};
}
function writeKeys(keys, directory = shared) {
  fs.mkdirSync(directory, { recursive: true });
  const file = path.join(directory, 'credentials.machine');
  fs.writeFileSync(file + '.tmp', crypto('encrypt', JSON.stringify(keys)));
  fs.renameSync(file + '.tmp', file);
}
function configure(app, testing = false) {
  const profile = testing ? shared : path.join(shared, 'browser');
  fs.mkdirSync(profile, { recursive: true });
  app.setPath('userData', profile);
  app.setPath('sessionData', profile);
  app.setPath('crashDumps', path.join(profile, 'crashes'));
  app.setAppLogsPath(path.join(profile, 'logs'));
  const temp = path.join(testing ? profile : shared, 'temp');
  fs.mkdirSync(temp, { recursive: true });
  app.setPath('temp', temp);
  process.env.TEMP = process.env.TMP = temp;
  return testing ? profile : shared;
}
module.exports = { root, shared, locations, readKeys, writeKeys, configure };
