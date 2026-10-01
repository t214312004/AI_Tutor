// Run once from the original Windows account. Never return decrypted keys.
import { _electron as electron } from 'playwright';
const app = await electron.launch({
  args: ['electron/credential-host.cjs', '--migrate-legacy'],
  cwd: process.cwd(),
});
try {
  console.log(await app.evaluate(() => globalThis.migrateLegacyTutorData()));
} finally {
  await app.close();
}
