const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

function absolute(value) {
  if (typeof value !== 'string' || !path.isAbsolute(value))
    throw Error('資料位置必須是絕對路徑。');
  return path.resolve(value);
}

function resolveStorage(root, env = process.env, testing = false) {
  if (testing) {
    const home = env.TUTOR_TEST_HOME ? absolute(env.TUTOR_TEST_HOME) : path.join(root, '.local/electron-test');
    return { home, data: path.join(home, 'data'), curriculum: path.join(home, 'curriculum') };
  }
  let local = {};
  const file = path.join(root, 'app-data/local-settings.json');
  if (!env.TUTOR_HOME && !env.TUTOR_DATA_DIR && fs.existsSync(file)) {
    local = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (!fs.existsSync(absolute(local.home)) || !fs.statSync(local.home).isDirectory())
      throw Error('本機資料位置無法存取，請檢查私有啟動設定。');
  }
  const home = env.TUTOR_HOME ? absolute(env.TUTOR_HOME)
    : env.TUTOR_DATA_DIR ? path.dirname(absolute(env.TUTOR_DATA_DIR))
    : local.home ? absolute(local.home)
    : path.join(env.LOCALAPPDATA || path.join(os.homedir(), '.local/share'), 'AI-Tutor');
  return {
    home,
    data: env.TUTOR_DATA_DIR ? absolute(env.TUTOR_DATA_DIR) : path.join(home, 'data'),
    curriculum: env.TUTOR_CURRICULUM_DIR ? absolute(env.TUTOR_CURRICULUM_DIR)
      : local.curriculum_dir ? absolute(local.curriculum_dir) : path.join(home, 'curriculum'),
  };
}

module.exports = { resolveStorage };
