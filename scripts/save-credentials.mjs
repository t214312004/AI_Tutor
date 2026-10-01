import { _electron as electron } from 'playwright';
let input = '';
for await (const chunk of process.stdin) input += chunk;
let app;
try {
  const keys = JSON.parse(input.replace(/^\uFEFF/, ''));
  input = '';
  app = await electron.launch({ args: ['electron/credential-host.cjs'], cwd: process.cwd() });
  const result = await app.evaluate(
    (_, provided) => globalThis.writeTutorCredentials(provided),
    keys,
  );
  console.log(JSON.stringify(result));
} catch {
  console.error('Credential save failed; secret details suppressed');
  process.exitCode = 1;
} finally {
  if (app) await app.close();
}
