import { defineConfig } from '@playwright/test';

/**
 * WP8-live smoke suite (issue #132). Runs against the live compose stack:
 * the API must be up on BASE_URL (default http://127.0.0.1:8200) and
 * CLAMS_API_TOKEN must be exported (same value as the API's).
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  workers: 1, // tests mutate shared live state (pause, ad-hoc, questions)
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: process.env.BASE_URL ?? 'http://127.0.0.1:8200',
    trace: 'off',
  },
});
