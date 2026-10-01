import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
export default defineConfig({
  plugins: [react()],
  base: './',
  build: { assetsInlineLimit: 0 },
  server: { strictPort: true },
  test: { include: ['src/**/*.test.ts', 'src/**/*.test.tsx'] },
});
