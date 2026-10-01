const { app, safeStorage } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const storage = require('./shared-storage.cjs');
app.setName('HomeworkCompanion');
const legacy = app.getPath('userData');
const migrating = process.argv.includes('--migrate-legacy');
if (!migrating) storage.configure(app);
globalThis.readTutorCredentials = () => storage.readKeys();
globalThis.writeTutorCredentials = (provided) => {
  const current = storage.readKeys();
  for (const name of ['GROQ_API_KEY', 'OPENAI_API_KEY', 'GEMINI_API_KEY'])
    if (typeof provided[name] === 'string' && provided[name].length < 300)
      current[name] = provided[name];
  storage.writeKeys(current);
  return { saved: true, providers: Object.keys(provided), storage: 'Windows LocalMachine DPAPI' };
};
globalThis.migrateLegacyTutorData = () => {
  if (!migrating) throw Error('Migration mode required');
  const profile = path.join(storage.shared, 'browser');
  const marker = path.join(storage.shared, 'migration.json');
  if (fs.existsSync(marker)) return { migrated: false, reason: 'already migrated' };
  fs.mkdirSync(storage.shared, { recursive: true });
  const oldKey = path.join(legacy, 'credentials.bin');
  if (fs.existsSync(oldKey) && !fs.existsSync(path.join(storage.shared, 'credentials.machine'))) {
    storage.writeKeys(JSON.parse(safeStorage.decryptString(fs.readFileSync(oldKey))));
  }
  const data = path.join(storage.shared, 'data');
  if (fs.existsSync(path.join(legacy, 'data'))) {
    if (fs.existsSync(data)) throw Error('Shared data already exists; refusing to overwrite');
    fs.cpSync(path.join(legacy, 'data'), data, {
      recursive: true,
      errorOnExist: true,
      force: false,
    });
  }
  // Only camera localStorage; no old Chromium encryption/profile identity.
  const local = path.join(legacy, 'Local Storage');
  if (fs.existsSync(local)) {
    fs.mkdirSync(profile, { recursive: true });
    if (!fs.existsSync(path.join(profile, 'Local Storage')))
      fs.cpSync(local, path.join(profile, 'Local Storage'), { recursive: true });
  }
  fs.writeFileSync(
    marker,
    JSON.stringify({ migratedAt: new Date().toISOString(), originalPreserved: true }),
  );
  return {
    migrated: true,
    credentials: Object.keys(storage.readKeys()).length,
    originalPreserved: true,
  };
};
app.whenReady();
