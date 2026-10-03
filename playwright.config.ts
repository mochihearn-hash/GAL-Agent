import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests', timeout: 90000, workers: 1, fullyParallel: false,
  reporter: [['list']], outputDir: 'test-results/playwright',
  use: { trace: 'retain-on-failure' }
});
