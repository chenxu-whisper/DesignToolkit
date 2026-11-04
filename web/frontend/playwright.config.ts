import { defineConfig, devices } from '@playwright/test'

const localPort = process.env.DESIGNTOOLKIT_E2E_LOCAL_PORT ?? '5173'
const hostedPort = process.env.DESIGNTOOLKIT_E2E_HOSTED_PORT ?? '5175'
const localUrl = `http://127.0.0.1:${localPort}`
const hostedUrl = `http://127.0.0.1:${hostedPort}`

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
        baseURL: localUrl,
      },
    },
    {
      name: 'hosted',
      testMatch: /hosted\.spec\.ts/,
      use: {
        ...devices['Desktop Chrome'],
        baseURL: hostedUrl,
      },
    },
  ],
  webServer: [
    {
      command: `npm run dev -- --port ${localPort} --strictPort`,
      env: {
        ...process.env,
        VITE_HOSTED_MODE: 'false',
      },
      url: localUrl,
      reuseExistingServer: false,
    },
    {
      command: `npm run dev -- --port ${hostedPort} --strictPort`,
      env: {
        ...process.env,
        VITE_HOSTED_MODE: 'true',
      },
      url: hostedUrl,
      reuseExistingServer: false,
    },
  ],
})
