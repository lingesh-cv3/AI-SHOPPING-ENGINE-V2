import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  // The transcript tests trigger one real model call each, and the free Groq
  // tier throttles at roughly four turns a minute - a slow response is normal
  // load, not a hang.
  timeout: 90000,
  // Serial, not parallel: every test drives the same demo engine and DB, and
  // several tests hit the model (throttled to ~4 turns/minute on the free
  // Groq tier). Parallel workers raced cart bootstrap and burned the model
  // budget across tests that never needed to overlap.
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: 'html',
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:5173',
    reuseExistingServer: !process.env.CI,
    timeout: 120000,
  },
});