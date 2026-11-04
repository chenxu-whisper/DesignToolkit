import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  testMatch: /local-drop\.spec\.ts/,
  workers: 1,
  outputDir: '../../.temp/playwright/local-drop-results',
  reporter: [['list']],
  use: {
    ...devices['Desktop Chrome'],
    baseURL: process.env.DESIGNTOOLKIT_LOCAL_DROP_URL ?? 'http://127.0.0.1:8765',
    trace: 'retain-on-failure',
  },
})
