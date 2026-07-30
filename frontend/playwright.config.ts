import { defineConfig, devices } from '@playwright/test'
import { isAbsolute, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

if (process.env.WORKBENCH_URL) {
  throw new Error('WORKBENCH_URL is forbidden for E2E: Playwright must never target a reusable Workbench server')
}

const e2ePort = Number(process.env.WORKBENCH_E2E_PORT ?? '18766')
if (!Number.isInteger(e2ePort) || e2ePort < 1024 || e2ePort > 65535 || e2ePort === 8765) {
  throw new Error('WORKBENCH_E2E_PORT must be a dedicated non-production port between 1024 and 65535')
}
const e2eRunId = process.env.WORKBENCH_E2E_RUN_ID ?? `${Date.now()}-${process.pid}`
if (!/^[A-Za-z0-9._-]{1,128}$/.test(e2eRunId)) {
  throw new Error('WORKBENCH_E2E_RUN_ID contains unsafe characters')
}
const projectRoot = fileURLToPath(new URL('..', import.meta.url))
const defaultE2eBase = fileURLToPath(new URL('../../valencia-workbench-test-state/e2e/', import.meta.url))
const e2eRoot = resolve(
  process.env.WORKBENCH_E2E_ROOT
    ?? `${process.env.WORKBENCH_E2E_BASE ?? defaultE2eBase}/valencia-workbench-e2e-${e2eRunId}`,
)
if (!isAbsolute(e2eRoot) || e2eRoot === projectRoot || e2eRoot.startsWith(`${projectRoot}${sep}`)) {
  throw new Error('WORKBENCH_E2E_ROOT must be absolute and outside the project tree')
}
const e2eURL = `http://127.0.0.1:${e2ePort}`
process.env.WORKBENCH_E2E_ROOT = e2eRoot
process.env.WORKBENCH_E2E_RUN_ID = e2eRunId
const shellQuote = (value: string) => `'${value.replaceAll("'", "'\"'\"'")}'`

export default defineConfig({
  testDir: './e2e',
  timeout: 90_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  globalSetup: './e2e/global-setup.ts',
  globalTeardown: './e2e/global-teardown.ts',
  webServer: {
    command: `env PYTHONPATH=../src WORKBENCH_ENV=test WORKBENCH_PORT=${e2ePort} WORKBENCH_TEST_ROOT=${shellQuote(e2eRoot)} WORKBENCH_TEST_RUN_ID=${shellQuote(e2eRunId)} WORKBENCH_TEST_REQUIRE_HEADER=1 WORKBENCH_VAR_DIR=${shellQuote(`${e2eRoot}/var`)} WORKBENCH_DB_PATH=${shellQuote(`${e2eRoot}/var/workbench.sqlite3`)} WORKBENCH_PREVIEW_ROOT=${shellQuote(`${e2eRoot}/previews`)} WORKBENCH_IMPORT_ROOT=${shellQuote(`${e2eRoot}/imports`)} WORKBENCH_RUN_ROOT=${shellQuote(`${e2eRoot}/runs`)} WORKBENCH_EXPORT_ROOT=${shellQuote(`${e2eRoot}/exports`)} MPLCONFIGDIR=${shellQuote(`${e2eRoot}/matplotlib`)} TMPDIR=${shellQuote(`${e2eRoot}/tmp`)} TMP=${shellQuote(`${e2eRoot}/tmp`)} TEMP=${shellQuote(`${e2eRoot}/tmp`)} ../.venv/bin/python -m workbench`,
    url: `${e2eURL}/api/health`,
    reuseExistingServer: false,
    timeout: 60_000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
  use: {
    baseURL: e2eURL,
    channel: 'chrome',
    extraHTTPHeaders: { 'X-Workbench-Test-Run': e2eRunId },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'mac-1280', use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } } },
    { name: 'mac-1440', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
  ],
})
