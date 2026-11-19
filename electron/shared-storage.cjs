const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const { resolveStorage } = require('./storage-paths.cjs');
const testing = process.argv.some(arg => ['--smoke', '--device-probe', '--test-mode'].includes(arg)) || process.env.TUTOR_TEST_MODE === '1';
const locations = resolveStorage(root, process.env, testing);
const shared = locations.home;
function configure(app, isTesting = testing) {
  const profile = isTesting ? shared : path.join(shared, 'browser');
  fs.mkdirSync(profile, { recursive: true });
  app.setPath('userData', profile);
  app.setPath('sessionData', profile);
  app.setPath('crashDumps', path.join(profile, 'crashes'));
  app.setAppLogsPath(path.join(profile, 'logs'));
  const temp = path.join(shared, 'temp');
  fs.mkdirSync(temp, { recursive: true });
  app.setPath('temp', temp);
  process.env.TEMP = process.env.TMP = temp;
  return shared;
}
module.exports = { root, locations, configure, testing };
