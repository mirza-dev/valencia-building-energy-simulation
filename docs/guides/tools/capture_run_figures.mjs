#!/usr/bin/env node
/** Capture publication-grade Run-page figures without controlling a real run.
 *
 * The final Workbench bundle is loaded from the live loopback server, while
 * only the Run-page read APIs are fulfilled from the reviewed, path-scrubbed
 * documentation fixture. No start, stop, resume, upload, or settings mutation
 * request is allowed to reach the server.
 */

import fs from 'node:fs/promises'
import path from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'
import { chromium } from '../../../frontend/node_modules/playwright/index.mjs'

const toolDir = path.dirname(fileURLToPath(import.meta.url))
const projectRoot = path.resolve(toolDir, '..', '..', '..')
const sourceDir = path.join(projectRoot, 'docs', 'guides', 'source')
const fixture = JSON.parse(await fs.readFile(path.join(sourceDir, 'fixtures', 'all-valenc-a-real-running.json'), 'utf8'))
const outputDir = path.join(sourceDir, 'capture-sources')
const baseURL = process.env.BSEW_BASE_URL || 'http://127.0.0.1:8765'

await fs.mkdir(outputDir, { recursive: true })

async function getJson(route) {
  const response = await fetch(`${baseURL}${route}`)
  if (!response.ok) throw new Error(`${route} returned ${response.status}`)
  return response.json()
}

const realSettings = await getJson('/api/project/settings')
const allDatasets = await getJson('/api/datasets')
const profile = await getJson('/api/stock/profile')
if (profile?.missing_inputs?.length) throw new Error(`active profile is not ready: ${profile.missing_inputs.join(', ')}`)

const datasetList = Array.isArray(allDatasets) ? allDatasets : allDatasets.datasets
const datasetById = new Map(datasetList.map((item) => [item.id, item]))
const valenciaWeatherId = 'managed-d2a508e9e6a6b96a67f4'
const valenciaDdyId = 'managed-6826bc4a24ac5c4d7d83'
const valenciaSettings = {
  ...realSettings,
  city_name: 'VALENCIA',
  weather_dataset_id: valenciaWeatherId,
  ddy_dataset_id: valenciaDdyId,
  microclimate_dataset_id: null,
  ground_temperature_c: 18.0,
  water_mains_temperature_c: 10.0,
  datasets: {
    ...realSettings.datasets,
    weather_dataset_id: datasetById.get(valenciaWeatherId),
    ddy_dataset_id: datasetById.get(valenciaDdyId),
    microclimate_dataset_id: null,
  },
}

const preflight = {
  ok: true,
  scope: 'all',
  buildings_in_scope: 26445,
  runnable: 26407,
  excluded: 38,
  exclusion_reasons: {
    'geometry excluded before simulation': 29,
    'missing supported stock evidence': 9,
  },
  estimated_minutes: 7099.2,
  estimated_seconds_per_building: 96.8,
  estimated_rate_basis: 'measured from the latest comparable annual full-stock run',
  estimated_bytes: 142807662592,
  stock_source_fingerprint: '05736c8cbe4ad5f0d6d3d44c56953a33e81c76f62327b1ed25385314f4c9bdb4',
}

const runningList = {
  runs: [{
    run: 'ALL_VALENC-A_REAL',
    modified: 1787869312.157946,
    has_summary: false,
    buildings: fixture.response.progress.counts.ok,
    running: true,
  }],
}

const runningDetail = {
  running: true,
  summary_is_partial: true,
  progress: fixture.response.progress,
  scope: fixture.response.scope,
  summary: fixture.response.summary,
}

const stoppedDetail = {
  ...runningDetail,
  running: false,
  summary_is_partial: true,
}

const logText = [
  'ALL_VALENC-A_REAL — durable execution monitor',
  'Six workers active; each terminal ledger record is flushed to disk.',
  'EnergyPlus building jobs continue under the verified profile cbf17543cfee3ff1.',
  'Operational progress evidence only — final aggregate not yet published.',
].join('\n')

function jsonResponse(route, value) {
  return route.fulfill({
    status: 200,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(value),
  })
}

async function installRunRoutes(page, state) {
  await page.route('**/api/project/settings', (route) => jsonResponse(route, valenciaSettings))
  await page.route('**/api/stock/preflight', async (route) => {
    if (route.request().method() !== 'POST') return route.abort('blockedbyclient')
    return jsonResponse(route, preflight)
  })
  await page.route('**/api/stock/runs', (route) => jsonResponse(route, state === 'preflight' ? { runs: [] } : runningList))
  await page.route('**/api/stock/runs/ALL_VALENC-A_REAL/log', (route) => route.fulfill({ status: 200, contentType: 'text/plain; charset=utf-8', body: logText }))
  await page.route('**/api/stock/runs/ALL_VALENC-A_REAL', (route) => jsonResponse(route, state === 'stopped' ? stoppedDetail : runningDetail))
  await page.route('**/api/stock/runs/**/stop', (route) => route.abort('blockedbyclient'))
  await page.route('**/api/stock/run', (route) => route.abort('blockedbyclient'))
}

async function captureValenciaFilesOverview(browser) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
  const page = await context.newPage()
  await page.route('**/api/project/settings', (route) => jsonResponse(route, valenciaSettings))
  await page.goto(`${baseURL}/#/files`, { waitUntil: 'networkidle' })
  await page.getByRole('heading', { name: 'Files', exact: true }).waitFor()
  await page.getByRole('button', { name: 'CADASTRE + LEDGER' }).click()
  await page.screenshot({ path: path.join(outputDir, 'files-overview.png') })
  await context.close()
}

async function dismissNotice(page) {
  const close = page.locator('.toast button')
  if (await close.count()) await close.last().click()
}

async function captureLeccoFilesDetails(browser) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
  const page = await context.newPage()
  await page.goto(`${baseURL}/#/files`, { waitUntil: 'networkidle' })
  await page.getByRole('heading', { name: 'Files', exact: true }).waitFor()

  await page.getByRole('button', { name: 'PREPARED STOCK' }).click()
  await dismissNotice(page)
  await page.screenshot({ path: path.join(outputDir, 'files-prepared-stock.png') })

  await page.getByRole('button', { name: 'BUILDING DATABASE' }).click()
  await dismissNotice(page)
  await page.screenshot({ path: path.join(outputDir, 'files-building-database.png') })

  await page.getByText('Microclimate slice', { exact: true }).scrollIntoViewIfNeeded()
  await dismissNotice(page)
  await page.screenshot({ path: path.join(outputDir, 'files-climate-event-chain.png') })
  await context.close()
}

async function openRunPage(browser, state) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
  const page = await context.newPage()
  await installRunRoutes(page, state)
  await page.goto(`${baseURL}/#/run`, { waitUntil: 'networkidle' })
  await page.getByRole('heading', { name: 'Run', exact: true }).waitFor()
  return { context, page }
}

const browser = await chromium.launch({ headless: true })
try {
  await captureValenciaFilesOverview(browser)
  await captureLeccoFilesDetails(browser)
  {
    const { context, page } = await openRunPage(browser, 'preflight')
    await page.getByRole('radio', { name: /All VALENCIA/ }).click()
    await page.getByLabel('RUN NAME').fill('ALL_VALENC-A_REAL')
    await page.getByRole('button', { name: 'Run preflight' }).click()
    await page.getByText('PREFLIGHT PASSED').waitFor()
    await page.getByText('I reviewed the scope').click()
    await page.screenshot({ path: path.join(outputDir, 'run-preflight.png') })
    await context.close()
  }

  {
    const { context, page } = await openRunPage(browser, 'running')
    await page.getByText('RUNNING', { exact: true }).waitFor()
    await page.screenshot({ path: path.join(outputDir, 'run-live-ledger.png') })
    await context.close()
  }

  {
    const { context, page } = await openRunPage(browser, 'stopped')
    await page.getByRole('radio', { name: /All VALENCIA/ }).click()
    await page.getByLabel('RUN NAME').fill('ALL_VALENC-A_REAL')
    await page.getByRole('button', { name: 'Run preflight' }).click()
    await page.getByText('PREFLIGHT PASSED').waitFor()
    await page.getByText('I reviewed the scope').click()
    await page.screenshot({ path: path.join(outputDir, 'run-stop-resume-protection.png') })
    await context.close()
  }
} finally {
  await browser.close()
}

console.log('RUN_FIGURE_CAPTURE_OK')
