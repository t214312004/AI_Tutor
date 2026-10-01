// Some restricted Windows test hosts cannot start Electron's sandboxed renderer.
// This opt-in flag applies only to test launchers; the desktop app remains unchanged.
export const electronTestFlags = process.env.TUTOR_TEST_NO_SANDBOX === '1' ? ['--no-sandbox'] : [];
