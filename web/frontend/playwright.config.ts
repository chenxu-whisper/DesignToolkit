import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  outputDir: '../../.temp/playwright/test-results',
  reporter: [['html', { open: 'never', outputFolder: '../../.temp/playwright/report' }]],
  use: {
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'local',
      testIgnore: /hosted\.spec\.ts/,
      use: {
        ...devices['Desktop Chrome'],
        baseURL: 'http://127.0.0.1:5173',
      },
    },
    {
      name: 'hosted',
      testMatch: /hosted\.spec\.ts/,
      use: {
        ...devices['Desktop Chrome'],
        baseURL: 'http://127.0.0.1:5175',
      },
    },
  ],
  webServer: [
    {
      command: 'npm run dev',
      url: 'http://127.0.0.1:5173',
      reuseExistingServer: true,
    },
    {
      command: 'npm run dev -- --port 5175',
      env: {
        ...process.env,
        VITE_HOSTED_MODE: 'true',
      },
      url: 'http://127.0.0.1:5175',
      reuseExistingServer: true,
    },
  ],
})
