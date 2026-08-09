import { expect, test, type APIRequestContext } from '@playwright/test'

async function ensurePipelineModel(request: APIRequestContext) {
  const existing = await request.get('/api/runs')
  const runs = await existing.json() as Array<{ id: string; run_type: string; provenance?: string }>
  const model = runs.find((item) => item.run_type === 'model' && item.provenance !== 'authored')
  if (model) return model
  const configResponse = await request.get('/api/config/schema')
  const config = (await configResponse.json() as { default: Record<string, unknown> }).default
  const geometryResponse = await request.post('/api/geometry/validate', { data: { building_ref: '4252702YJ2745A', config } })
  expect(geometryResponse.status()).toBe(200)
  const geometry = await geometryResponse.json() as { actions: Array<Record<string, unknown>> }
  const previewResponse = await request.post('/api/previews', { data: {
    building_ref: '4252702YJ2745A', config,
    geometry_actions: geometry.actions.map((item) => ({ ...item, approved: true })),
  } })
  expect(previewResponse.status()).toBe(202)
  const previewId = (await previewResponse.json() as { job_id: string }).job_id
  let status = ''
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const detail = await request.get(`/api/previews/${previewId}`)
    status = (await detail.json() as { job: { status: string } }).job.status
    if (status === 'ready') break
    if (['failed', 'canceled'].includes(status)) throw new Error(`Base model preview failed: ${status}`)
    await new Promise((resolve) => setTimeout(resolve, 500))
  }
  expect(status).toBe('ready')
  const committed = await request.post(`/api/previews/${previewId}/commit`, { data: {} })
  expect(committed.status()).toBe(200)
  return await committed.json() as { id: string; run_type: string; provenance?: string }
}

const stockRunOptions = (districts: string[] = []) => ({
  features: {
    district_scope: districts.length > 0,
    single_building: true,
    comfort_scenario: true,
    weather_scenario: true,
    real_hvac_consumption: true,
  },
  districts,
  weather_datasets: [{
    id: 'future-epw', name: 'Valencia future', source_name: 'Valencia_2050.epw',
    snapshot_hash: '9'.repeat(64),
  }],
  delta_bounds_c: { min: -3, max: 3, step: 0.1 },
  source_types: ['human_judgement', 'dataset', 'publication', 'supervisor', 'other'],
})

function cityFixture(invalid = false) {
  const now = new Date().toISOString()
  const districtNames = ["L'EIXAMPLE", 'POBLATS MARITIMS', 'EXTRAMURS', 'QUATRE CARRERES', 'CIUTAT VELLA', 'CAMPANAR', 'BENICALAP', 'PATRAIX', 'JESUS', 'ALGIRÓS', 'CAMINS AL GRAU', 'RASCANYA', 'OLIVERETA', 'SAÏDIA', 'PLA DEL REAL', 'POBLATS DEL NORD', 'POBLATS DEL SUD', "L'ALBUFERA", 'POBLATS DE L’OEST']
  const clusterNames = ['BlocPluriP01', 'BlocPluriP02', 'BlocPluriP03', 'BlocPluriP04', 'BlocPluriP05', 'BlocPluriP06', 'BlocPluriP07', 'EdiPluriP01', 'EdiPluriP02', 'EdiPluriP03', 'EdiPluriP04', 'EdiPluriP05', 'EdiPluriP06', 'EdiPluriP07', 'VivUniP01', 'VivUniP02', 'VivUniP03', 'VivUniP04', 'VivUniP05', 'VivUniP06', 'VivUniP07']
  const representatives = clusterNames.map((cluster, index) => ({
    cluster, family: cluster.replace(/P\d+$/, ''), period: cluster.match(/P\d+$/)?.[0] ?? 'P01', refparcela: `REP${index + 1}`,
    n_buildings: 500 + index * 37, rep_area_m2: 120 + index * 11, cluster_med_area_m2: 118 + index * 11,
    rep_floors: 2 + index % 6, cluster_med_floors: 2 + index % 6, rep_vertices: 4 + index % 4,
  }))
  const clusters = representatives.map((item, index) => ({ ...item, qa_all_pass: true, qa_all_pass_hvac: true, heating_kwh_m2: 4.3 + index * 0.7, cooling_kwh_m2: 10.4 + index * 0.6, cons_hc_kwh_m2: 13.7 + index * 0.75, total_site_kwh_m2: 40.1 + index * 0.8, hvac_co2_kg_m2: 3.9 + index * 0.2, total_site_co2_kg_m2: 13.5 + index * 0.2, s1_co2_kg_m2: 7.2, s2_co2_kg_m2: 4.1, eplus_warnings: 11, param_wall_u: 1.33, param_roof_u: 1.92, param_window_u: 5.7, param_window_g: 0.82, param_ground_unconditioned: true }))
  const districts = districtNames.map((nombre, index) => ({ nombre, coddistrit: index + 1, n_buildings: 900 + index * 55, res_area_m2: 950000 + index * 120000, longitude: -0.42 + (index % 5) * 0.03, latitude: 39.31 + Math.floor(index / 5) * 0.065, heating_gwh: 20 + index * 1.9, cooling_gwh: 23 + index * 1.7, cons_hc_gwh: 64.2 - index * 1.8, total_site_gwh: 128 - index * 2.2, hvac_co2_t: 12000 - index * 210, total_site_co2_t: 32000 - index * 340, s1_co2_t: 11000 + index * 700, s2_co2_t: 6000 + index * 400 }))
  const totals = invalid ? null : { heating_gwh_yr: 596.41, cooling_gwh_yr: 712.02, s1_co2_t_yr: 315192, s2_co2_t_yr: 173333, hvac_consumption_gwh_yr: 709.28, total_site_gwh_yr: 1992.85, s1_consumption_gwh_yr: 1110.2, s2_consumption_gwh_yr: 817.4, hvac_co2_t_yr: 198598, total_site_co2_t_yr: 623521, residential_area_m2: 44406369 }
  const failures = invalid ? [{ cluster: 'BlocPluriP04', refparcela: 'REP4', stage: 'representative_simulation', error: 'EnergyPlus Severe city fixture' }] : []
  const result = {
    schema_version: 1, settings: { scope: 'Valencia', run_mode: 'full_baseline', method: 'representative_typology_period', capability_version: '1' },
    summary: { scope: 'Valencia', buildings: 26452, districts: invalid ? 0 : 19, clusters_expected: 21, clusters_completed: invalid ? 20 : 21, clusters_failed: invalid ? 1 : 0, qa_passed_clusters: invalid ? 20 : 21, totals, certificate_heating: invalid ? null : { buildings: 26428, model_kwh_m2: 13.43, certificate_kwh_m2: 24.87, ratio: 0.54, definition_status: 'CONDITIONAL_JAVIER_Q3' } },
    qa: { all_pass: !invalid, scientific_status: invalid ? 'INVALID' : 'VALIDATED', checks: [], failures, visualization_warning: null }, representatives, clusters: invalid ? [] : clusters, districts: invalid ? [] : districts, validation: invalid ? '' : 'Part D validation', map_available: true, energy_map_available: !invalid,
  }
  const run = { id: invalid ? 'invalid-city' : 'city-run', job_id: 'city-job', refparcela: 'VALENCIA', scenario_name: invalid ? 'Invalid Part D diagnostic' : 'Valencia · full Part D baseline', config: result.settings, stats: result.summary, qa: result.qa, artifact_dir: '/tmp/city', verification_status: 'VERIFIED', run_type: 'city', parent_run_id: null, created_at: now, artifacts: [{ name: 'results_buildings.gpkg', sha256: 'a'.repeat(64), size_bytes: 100 }], result, verification: { ok: true, status: 'VERIFIED', issues: [] } }
  const preflight = { schema_version: 1, scope: 'Valencia', method: 'representative_typology_period', locked: true, summary: { buildings: 26452, clusters: 21, representatives: 21, districts: 19, residential_area_m2: 44406369, imputed_floor_buildings: 1648, proxy_area_buildings: 192, duplicate_parcel_rows: 13 }, representatives, districts, bounds: [-0.4284, 39.2791, -0.2758, 39.5512], capability: { version: '1', runner_sha256: 'b'.repeat(64), adapter_sha256: 'c'.repeat(64) } }
  return { run, result, preflight, representatives, clusters, districts }
}

function lhsFixture() {
  const now = new Date().toISOString()
  const ranges: Array<[string, number, number, 'simulation' | 'post', string, string]> = [
    ['wall_u', 1.0, 2.0, 'simulation', 'W/m²K', 'envelope'],
    ['roof_u', 1.0, 2.0, 'simulation', 'W/m²K', 'envelope'],
    ['window_u', 4.0, 6.0, 'simulation', 'W/m²K', 'openings'],
    ['window_g', 0.6, 0.9, 'simulation', '—', 'openings'],
    ['infiltration_ach', 0.1, 0.5, 'simulation', '1/h', 'operation'],
    ['shade_setpoint', 100, 400, 'simulation', 'W/m²', 'operation'],
    ['thermal_bridge_du', 0, 0.2, 'simulation', 'W/m²K', 'envelope'],
    ['cop', 1, 3, 'post', '—', 'system'],
    ['seer', 1.8, 3.5, 'post', '—', 'system'],
    ['emission_factor', 0.15, 0.33, 'post', 'kgCO₂/kWh', 'carbon'],
  ]
  const variables = ranges.map(([name, minimum, maximum, group, unit, domain]) => ({ name, minimum, maximum, group, unit, domain, distribution: 'uniform' as const }))
  const samples = Array.from({ length: 50 }, (_, index) => {
    const fraction = (index + 0.5) / 50
    const values = Object.fromEntries(ranges.map(([name, minimum, maximum]) => [name, minimum + (maximum - minimum) * fraction]))
    return { ...values, heating_kwh_m2: 12.38 + index * 0.2075, cooling_kwh_m2: 13.68 + index * 0.0385, consumption_kwh_m2: 10.2 + index * 0.18, co2_kg_m2: 2.08 + index * 0.0767, co2_t_building: 5.84 + index * 0.215 }
  })
  const statistics = {
    heating_kwh_m2: { mean: 16.6562, median: 16.595, p5: 12.382, p95: 22.5485, minimum: 11.92, maximum: 23.41, stddev: 3.05 },
    cooling_kwh_m2: { mean: 14.6308, median: 14.59, p5: 13.6815, p95: 15.5665, minimum: 13.51, maximum: 15.91, stddev: 0.61 },
    co2_kg_m2: { mean: 3.59, median: 3.29, p5: 2.0795, p95: 5.8385, minimum: 1.91, maximum: 6.24, stddev: 1.21 },
  }
  const sensitivity = {
    heating_kwh_m2: [{ variable: 'infiltration_ach', rho: 0.8471, rank: 1 }, { variable: 'wall_u', rho: 0.3364, rank: 2 }, { variable: 'thermal_bridge_du', rho: 0.3068, rank: 3 }],
    cooling_kwh_m2: [{ variable: 'shade_setpoint', rho: 0.7027, rank: 1 }, { variable: 'wall_u', rho: 0.5548, rank: 2 }, { variable: 'infiltration_ach', rho: 0.2706, rank: 3 }],
    co2_kg_m2: [{ variable: 'emission_factor', rho: 0.6177, rank: 1 }, { variable: 'cop', rho: -0.5725, rank: 2 }, { variable: 'infiltration_ach', rho: 0.4007, rank: 3 }],
  }
  const checks = ['sample_count', 'result_schema', 'finite_values', 'variable_ranges', 'lhs_stratification', 'nonnegative_outputs', 'sensitivity_top3', 'core_artifacts'].map((name) => ({ name, passed: true, expected: true, actual: true }))
  const result = {
    schema_version: 1,
    settings: { scope: '4252702YJ2745A', run_mode: 'frozen_baseline', method: 'latin_hypercube_uniform_spearman', n: 50, seed: 42, capability_version: '1' },
    summary: { scope: '4252702YJ2745A', samples_expected: 50, samples_completed: 50, simulation_variables: 7, post_variables: 3, statistics },
    qa: { all_pass: true, scientific_status: 'VALIDATED', checks }, variables, variable_fingerprint: 'd'.repeat(64),
    baselines: { massless: { heating: 16.67, cooling: 18.67 }, layered: { heating: 11.21, cooling: 16.6 }, cadastre: { heating: 27.97, cooling: 6.63 } },
    sensitivity, samples, figures: { distributions: 'histograms.png', sensitivity: 'tornado.png' }, summary_text: 'LHS fixture summary\n50/50 samples completed.',
  }
  const makeRun = (id: string, hours: number) => ({
    id, job_id: `${id}-job`, refparcela: '4252702YJ2745A', scenario_name: 'Pilot · N50 seed42', config: result.settings,
    stats: result.summary, qa: result.qa, artifact_dir: `/tmp/${id}`, verification_status: 'VERIFIED', run_type: 'lhs', parent_run_id: null,
    created_at: new Date(Date.parse(now) - hours * 3_600_000).toISOString(), artifacts: [{ name: 'runs.csv', sha256: 'a'.repeat(64), size_bytes: 1200 }],
    result, verification: { ok: true, status: 'VERIFIED', issues: [] },
  })
  const runs = [makeRun('lhs-run', 0), makeRun('lhs-run-previous', 24)]
  const preflight = { schema_version: 1, scope: '4252702YJ2745A', method: 'latin_hypercube_uniform_spearman', locked: true, settings: { n: 50, seed: 42, simulation_variables: 7, post_variables: 3, model_path: 'massless', context_shading: true, estimated_minutes: 20 }, variables, variable_fingerprint: 'd'.repeat(64), baselines: result.baselines, outputs: Object.keys(statistics), capability: { version: '1', runner_sha256: 'b'.repeat(64), adapter_sha256: 'c'.repeat(64) } }
  return { preflight, result, runs }
}

let runtimeIdentityVerified = false

test.beforeEach(async ({ request }) => {
  if (runtimeIdentityVerified) return
  const response = await request.get('/api/health')
  expect(response.status()).toBe(200)
  const payload = await response.json() as {
    environment: {
      mode: string
      port: number
      test_run_id: string | null
      test_root: string | null
      test_request_header_required: boolean
      mutable_paths: Record<string, string>
    }
  }
  const expectedRoot = process.env.WORKBENCH_E2E_ROOT
  expect(payload.environment.mode).toBe('test')
  expect(payload.environment.port).not.toBe(8765)
  expect(payload.environment.test_run_id).toBe(process.env.WORKBENCH_E2E_RUN_ID)
  expect(payload.environment.test_root).toBe(expectedRoot)
  expect(payload.environment.test_request_header_required).toBe(true)
  expect(expectedRoot).toMatch(/\/valencia-workbench-e2e-[^/]+$/)
  for (const path of Object.values(payload.environment.mutable_paths)) {
    expect(path.startsWith(`${expectedRoot}/`)).toBe(true)
  }
  runtimeIdentityVerified = true
})

test.afterEach(async ({ request }) => {
  try {
    for (const endpoint of ['/api/simulations/jobs/active', '/api/previews/jobs/active']) {
      const response = await request.get(endpoint)
      if (!response.ok()) continue
      const payload = await response.json() as { job: { id: string; status: string } | null }
      const job = payload.job
      if (!job || !['queued', 'running', 'cancel_requested'].includes(job.status)) continue
      await request.post(`/api/jobs/${job.id}/cancel`)
      for (let attempt = 0; attempt < 40; attempt += 1) {
        const statusResponse = await request.get(`/api/jobs/${job.id}`)
        if (!statusResponse.ok()) break
        const status = await statusResponse.json() as { status: string }
        if (['completed', 'failed', 'canceled', 'timed_out'].includes(status.status)) break
        await new Promise((resolve) => setTimeout(resolve, 500))
      }
    }
  } catch {
    // A stopped test server needs no job cleanup.
  }
})

test('builder validates, previews exact geometry, commits, and exports', async ({ page }) => {
  const builderTileResponse = page.waitForResponse((response) => response.url().includes('/api/map/tiles/'))
  await page.goto('/#/builder')
  await expect(page.getByRole('heading', { name: /Bina modeli|Building model/ })).toBeVisible()
  const tileResponse = await builderTileResponse
  expect(tileResponse.status()).toBe(200)
  expect(new URL(tileResponse.url()).origin).toBe(new URL(page.url()).origin)
  await expect(page.locator('.map-panel')).toHaveAttribute('data-map-ready', 'true')
  await expect(page.locator('.map-panel')).toHaveAttribute('data-map-error', '')
  await page.locator('.page-actions .secondary-button').first().click()
  await expect(page.getByText('20.04')).toBeVisible()
  const approvals = page.locator('.geometry-actions input[type="checkbox"]')
  for (let index = 0; index < await approvals.count(); index += 1) {
    if (!(await approvals.nth(index).isChecked())) await approvals.nth(index).check()
  }

  await page.getByRole('button', { name: /Fizik|Physics/ }).click()
  const shgc = page.getByLabel(/Pencere g \/ SHGC|Window g \/ SHGC/)
  await shgc.fill('1.2')
  await shgc.blur()
  await expect(shgc).toHaveAttribute('aria-invalid', 'true')
  await expect(page.getByRole('button', { name: /Kesin önizleme üret|Build exact preview/ })).toBeDisabled()
  await shgc.fill('0.82')
  await shgc.blur()

  await page.getByRole('button', { name: /Kesin önizleme üret|Build exact preview/ }).click()
  await expect(page).toHaveURL(/#\/builder\?preview=[a-f0-9-]+/)
  const previewJobId = new URL(page.url()).hash.match(/[?&]preview=([^&]+)/)?.[1]
  expect(previewJobId).toBeTruthy()
  await page.reload()
  await expect(page).toHaveURL(new RegExp(`preview=${previewJobId}`))
  await expect(page.getByText('Preview ready for review')).toBeVisible({ timeout: 60_000 })
  const eventIds = await page.locator('.event-log [data-event-id]').evaluateAll((nodes) => nodes.map((node) => node.getAttribute('data-event-id')))
  expect(new Set(eventIds).size).toBe(eventIds.length)

  const reopened = await page.context().newPage()
  await reopened.goto('/#/builder')
  const recoverable = reopened.locator(`[data-preview-job="${previewJobId}"]`)
  await expect(recoverable).toBeVisible()
  await expect(recoverable).toHaveAttribute('data-preview-status', 'ready')
  await recoverable.click()
  await expect(reopened).toHaveURL(new RegExp(`preview=${previewJobId}`))
  await expect(reopened.getByText('Preview ready for review')).toBeVisible()
  await expect(reopened.locator('.model-viewer[data-render-ready="true"]')).toBeVisible()
  expect(await reopened.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  await reopened.close()

  await page.getByRole('button', { name: /3D model/ }).click()
  await expect(page.locator('.model-viewer[data-render-ready="true"]')).toBeVisible()
  const canvas = page.locator('canvas').first()
  await expect(canvas).toBeVisible()
  await expect(page.locator('.model-viewer')).toHaveAttribute('data-view-mode', 'overview')
  expect(Number(await page.locator('.model-viewer').getAttribute('data-context-roofs'))).toBeGreaterThan(0)
  const box = await canvas.boundingBox()
  expect(box?.width).toBeGreaterThan(300)
  expect(box?.height).toBeGreaterThan(300)
  expect(await canvas.evaluate((element) => (element as HTMLCanvasElement).toDataURL('image/png').length)).toBeGreaterThan(10_000)
  const pixelSummary = await canvas.evaluate((element) => {
    const canvasElement = element as HTMLCanvasElement
    const gl = canvasElement.getContext('webgl2') ?? canvasElement.getContext('webgl')
    if (!gl) return { sampled: 0, nonBackground: 0 }
    const pixels = new Uint8Array(canvasElement.width * canvasElement.height * 4)
    gl.readPixels(0, 0, canvasElement.width, canvasElement.height, gl.RGBA, gl.UNSIGNED_BYTE, pixels)
    let sampled = 0
    let nonBackground = 0
    for (let index = 0; index < pixels.length; index += 64) {
      sampled += 1
      if (pixels[index] < 238 || pixels[index + 1] < 238 || pixels[index + 2] < 238) nonBackground += 1
    }
    return { sampled, nonBackground }
  })
  expect(pixelSummary.sampled).toBeGreaterThan(1_000)
  expect(pixelSummary.nonBackground / pixelSummary.sampled).toBeGreaterThan(0.02)

  await page.locator('#cut-height').fill('9')
  await expect(page.getByText('9.0 m')).toBeVisible()
  await page.getByRole('button', { name: /party|ortak duvar/ }).click()
  await page.getByRole('button', { name: /party|ortak duvar/ }).click()
  for (const position of [{ x: 0.50, y: 0.50 }, { x: 0.58, y: 0.45 }, { x: 0.42, y: 0.55 }]) {
    const current = await canvas.boundingBox()
    if (!current) break
    await canvas.click({ position: { x: current.width * position.x, y: current.height * position.y } })
    if (await page.locator('.surface-inspector').count()) break
  }
  await expect(page.locator('.surface-inspector')).toBeVisible()
  await page.getByRole('button', { name: /Görünümü kaydet|Store view/ }).click()
  await expect(page.getByText(/kamera, kesit ve katman görünümü kaydedildi|camera, clipping and layer view stored/i)).toBeVisible()
  await page.getByRole('button', { name: /Görsel alanı büyüt|Expand visual workspace/ }).click()
  await expect(page.locator('.builder-workspace.canvas-fullscreen')).toBeVisible()
  await page.getByRole('button', { name: /Büyütülmüş görünümden çık|Exit expanded view/ }).click()

  await page.getByRole('button', { name: /Koşuyu kaydet|Commit run/ }).click()
  await expect(page.getByText(/Değiştirilemez koşu kaydedildi|Immutable run committed/)).toBeVisible()
  await page.goto('/#/runs')
  await expect(page.getByRole('link', { name: /Paketi indir|Download bundle/ })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
})

test('Phase E0 inspects the materialized OpenStudio graph and cross-highlights constructions', async ({ page, request }) => {
  const model = await ensurePipelineModel(request)

  const graphResponse = await request.get(`/api/models/${model!.id}/graph`)
  expect(graphResponse.status()).toBe(200)
  const payload = await graphResponse.json() as { graph: { graph_sha256: string; counts: Record<string, number>; project_parameters: Array<{ key: string; current_value: unknown; binding: { object_name: string | null } }> } }
  expect(payload.graph.graph_sha256).toMatch(/^[a-f0-9]{64}$/)
  expect(payload.graph.counts.constructions).toBeGreaterThan(0)
  expect(payload.graph.counts.materials).toBeGreaterThan(0)
  expect(payload.graph.counts.schedules).toBeGreaterThan(0)
  expect(payload.graph.counts.space_types).toBeGreaterThan(0)
  expect(payload.graph.counts.zones).toBeGreaterThan(0)
  expect(payload.graph.counts.project_parameters).toBe(13)
  expect(payload.graph.project_parameters.find((item) => item.key === 'infiltration_ach')).toMatchObject({
    current_value: 0.2,
    binding: { object_name: 'Infitracion Aire constante 0,2ACH Viv CTE' },
  })

  await page.goto(`/#/model/${model!.id}`)
  await expect(page.getByRole('heading', { name: /OpenStudio Model Editörü|OpenStudio Model Editor/ })).toBeVisible()
  await expect(page.locator('.model-viewer[data-render-ready="true"]')).toBeVisible()
  await expect(page.getByRole('tab')).toHaveCount(9)
  await expect(page.getByText('PlantillaOS_v2.osm')).toBeVisible()
  await expect(page.locator('[data-parameter-key="infiltration_ach"]')).toContainText('Infitracion Aire constante 0,2ACH Viv CTE')
  await expect(page.locator('.viewer-story-control')).not.toContainText(/\b(?:1th|2th|3th) floor\b/)
  const projectTab = page.getByRole('tab', { name: /Proje Parametreleri|Project Parameters/ })
  await expect(projectTab).toHaveAttribute('aria-controls', 'model-graph-tabpanel')
  await expect(projectTab).toHaveAttribute('aria-selected', 'true')
  await projectTab.focus()
  await projectTab.press('ArrowRight')
  const geometryTab = page.getByRole('tab', { name: /Geometri|Geometry/ })
  await expect(geometryTab).toBeFocused()
  await expect(geometryTab).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', 'model-graph-tab-geometry')
  await geometryTab.press('Home')
  await expect(projectTab).toBeFocused()

  await page.getByRole('tab', { name: /Konstrüksiyon|Constructions/ }).click()
  const firstConstruction = page.locator('button.model-object-card').first()
  await firstConstruction.click()
  const selectedId = await page.locator('.model-viewer').getAttribute('data-selected-construction')
  expect(selectedId).toMatch(/^\{[a-f0-9-]+\}$/)
  await expect(firstConstruction).toHaveClass(/active/)

  await page.getByRole('tab', { name: /Malzeme|Materials/ }).click()
  await page.getByPlaceholder(/Bu sekmede ara|Search this tab/).fill('IVE')
  await expect(page.locator('.model-object-card').first()).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)

  await expect(page.getByTitle('TR / EN')).toHaveCount(0)
  await expect(page.locator('html')).toHaveAttribute('lang', 'en')
})

test('Phase E1 authors wall and thermostat edits, recovers the session, and commits a Part B eligible model', async ({ page, request }) => {
  test.setTimeout(300_000)
  const model = await ensurePipelineModel(request)

  await page.goto(`/#/model/${model!.id}`)
  await page.getByTestId('start-model-editor').click()
  await expect(page.getByRole('heading', { name: /OpenStudio (Uzman Editörü|Expert Editor)/ })).toBeVisible()
  await expect(page.getByTestId('editor-mode-advanced')).toHaveClass(/active/)
  await expect(page.getByTestId('editor-mode-advanced')).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByTestId('editor-mode-guided')).toHaveAttribute('aria-pressed', 'false')
  await page.getByRole('tab', { name: /Mekân Tipleri|Space Types/ }).click()
  await expect(page.getByTestId('space-infiltration-value')).not.toHaveValue('')
  await page.getByRole('tab', { name: /Proje Param|Project Parameters/ }).click()

  await page.getByTestId('parameter-wall_u-input').fill('-1')
  await page.getByTestId('parameter-wall_u-apply').click()
  await expect(page.locator('.editor-report-list .error')).toContainText('rejected')
  await expect(page.getByText(/0 override uygulandı|0 overrides applied/)).toBeVisible()

  await page.getByTestId('parameter-wall_u-input').fill('1.5')
  await page.getByTestId('parameter-wall_u-apply').click()
  await expect(page.getByText(/1 override uygulandı|1 override applied/)).toBeVisible()
  await page.getByRole('tab', { name: /Zonlar|Zones/ }).click()
  await page.getByTestId('thermostat-heating-delta').fill('1')
  await page.getByTestId('thermostat-cooling-delta').fill('-1')
  await page.getByTestId('thermostat-apply').click()
  await expect(page.getByText(/2 override uygulandı|2 overrides applied/)).toBeVisible()

  await page.reload()
  await expect(page.getByRole('heading', { name: /OpenStudio (Uzman Editörü|Expert Editor)/ })).toBeVisible()
  await expect(page.getByText(/2 override uygulandı|2 overrides applied/)).toBeVisible()
  await page.getByRole('button', { name: /Preflight’ı incele|Review preflight/ }).click()
  await expect(page.getByText(/PREFLIGHT HAZIR|PREFLIGHT READY/)).toBeVisible()
  await page.getByTestId('commit-authored-model').click()
  await expect(page.getByText(/Authored varyant kaydedildi|Authored variant committed/)).toBeVisible()
  const authoredLink = page.getByRole('link', { name: /Authored modeli aç|Open authored model/ })
  const href = await authoredLink.getAttribute('href')
  const authoredId = href?.split('/').at(-1)
  expect(authoredId).toMatch(/^[a-f0-9]{32}$/)

  const run = await request.get(`/api/runs/${authoredId}`)
  expect(run.status()).toBe(200)
  expect(await run.json()).toMatchObject({ provenance: 'authored', authored_from: model!.id, verification_status: 'VERIFIED' })
  const verification = await request.post(`/api/runs/${authoredId}/verify`)
  expect(verification.status()).toBe(200)
  expect(await verification.json()).toMatchObject({ ok: true, status: 'VERIFIED' })
  const eligible = await request.get('/api/simulations/eligible-models')
  expect((await eligible.json()) as Array<{ id: string }>).toContainEqual(expect.objectContaining({ id: authoredId }))
  await page.getByRole('link', { name: /Part B ile simüle et|Simulate with Part B/ }).click()
  await expect(page).toHaveURL(new RegExp(`/#/simulation\\?model=${authoredId}$`))
  await expect(page.getByTestId('simulation-parent-model')).toHaveValue(authoredId)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
})

test('Phase E2 draws a window on the selected automatic model and commits changed glazing geometry', async ({ page, request }) => {
  test.setTimeout(150_000)
  const model = await ensurePipelineModel(request)

  await page.goto(`/#/model/${model!.id}`)
  await page.getByTestId('start-model-editor').click()
  await page.getByRole('tab', { name: /Geometri|Geometry/ }).click()
  await expect(page.getByTestId('geometry-editor')).toBeVisible()
  const before = Number(await page.getByTestId('geometry-opening-count').textContent())
  expect(before).toBeGreaterThan(0)

  await page.getByTestId('geometry-create-opening').click()
  await expect(page.getByText(/1 override uygulandı|1 override applied/)).toBeVisible()
  await expect(page.getByTestId('geometry-opening-count')).toHaveText(String(before + 1))
  await expect(page.locator('.editor-report-list')).toContainText('subsurface.create applied')

  await page.getByRole('button', { name: /Preflight’ı incele|Review preflight/ }).click()
  await expect(page.getByText(/PREFLIGHT HAZIR|PREFLIGHT READY/)).toBeVisible()
  await page.getByTestId('commit-authored-model').click()
  await expect(page.getByText(/Authored varyant kaydedildi|Authored variant committed/)).toBeVisible()
  const href = await page.getByRole('link', { name: /Authored modeli aç|Open authored model/ }).getAttribute('href')
  const authoredId = href?.split('/').at(-1)
  expect(authoredId).toMatch(/^[a-f0-9]{32}$/)

  const sourceResponse = await request.get(`/api/runs/${model!.id}`)
  const authoredResponse = await request.get(`/api/runs/${authoredId}`)
  const source = await sourceResponse.json() as { stats: { n_windows: number; window_area_m2: number; facade_qa: unknown[] } }
  const authored = await authoredResponse.json() as { stats: { n_windows: number; window_area_m2: number; facade_qa: unknown[] }; provenance: string; authored_from: string }
  expect(authored).toMatchObject({ provenance: 'authored', authored_from: model!.id })
  expect(authored.stats.n_windows).toBe(source.stats.n_windows + 1)
  expect(authored.stats.window_area_m2).toBeGreaterThan(source.stats.window_area_m2)
  expect(authored.stats.facade_qa).not.toEqual(source.stats.facade_qa)
})

test('Phase E3 authors a real HVAC loop and applies a provenance-retained OpenStudio Measure', async ({ page, request }) => {
  test.setTimeout(210_000)
  const model = await ensurePipelineModel(request)

  await page.goto(`/#/model/${model!.id}`)
  await page.getByTestId('start-model-editor').click()
  await page.getByRole('tab', { name: 'HVAC' }).click()
  await expect(page.getByText(/E3 \/ HVAC (LOOP’LARI|LOOPS) \+ MEASURES/)).toBeVisible()
  await page.getByText(/Tüm HVAC sistemini değiştir|Replace the complete HVAC system/).click()
  await page.getByTestId('hvac-system-select').selectOption('gas_furnace')
  await expect(page.getByTestId('heating-only-warning')).toContainText(/yalnız ısıtma|heating only/)
  await page.getByTestId('hvac-loop-template').selectOption('vav_reheat_dx')
  await page.getByTestId('hvac-create-loop').click()
  await expect(page.getByText(/1 override uygulandı|1 override applied/)).toBeVisible()
  await expect(page.locator('.hvac-loop-card')).toHaveCount(2)
  await expect(page.locator('.hvac-zone-branches button')).toHaveCount(5)

  const loopAuthoringTab = page.getByRole('tab', { name: /Loop düzenleme|Loop authoring/ })
  await expect(loopAuthoringTab).toHaveAttribute('aria-selected', 'true')
  await loopAuthoringTab.focus()
  await loopAuthoringTab.press('ArrowRight')
  await expect(page.getByTestId('measure-panel-tab')).toBeFocused()
  await expect(page.getByTestId('measure-panel-tab')).toHaveAttribute('aria-selected', 'true')
  await expect(page.locator('#hvac-tabpanel-measures')).toHaveAttribute('aria-labelledby', 'hvac-tab-measures')
  await expect(page.getByTestId('measure-select')).toHaveValue('builtin:set_building_north_axis')
  await page.getByTestId('measure-argument-north_axis_deg').fill('17.5')
  await page.getByTestId('measure-apply').click()
  await expect(page.getByText(/2 override uygulandı|2 overrides applied/)).toBeVisible({ timeout: 60_000 })
  await expect(page.locator('.editor-report-list')).toContainText('Measure Set Building North Axis applied')

  await page.getByRole('button', { name: /Preflight’ı incele|Review preflight/ }).click()
  await expect(page.getByText(/PREFLIGHT HAZIR|PREFLIGHT READY/)).toBeVisible()
  await page.getByTestId('commit-authored-model').click()
  await expect(page.getByText(/Authored varyant kaydedildi|Authored variant committed/)).toBeVisible()
  const href = await page.getByRole('link', { name: /Authored modeli aç|Open authored model/ }).getAttribute('href')
  const authoredId = href?.split('/').at(-1)
  expect(authoredId).toMatch(/^[a-f0-9]{32}$/)

  const graphResponse = await request.get(`/api/models/${authoredId}/graph`)
  const graph = await graphResponse.json() as { graph: { counts: Record<string, number>; hvac: { air_loops: Array<{ zones: unknown[] }>; plant_loops: unknown[] } } }
  expect(graph.graph.counts.air_loops).toBe(1)
  expect(graph.graph.counts.plant_loops).toBe(1)
  expect(graph.graph.hvac.air_loops[0].zones).toHaveLength(5)

  const runResponse = await request.get(`/api/runs/${authoredId}`)
  const run = await runResponse.json() as { artifacts: Array<{ name: string }>; patch_journal: Array<{ patch: { op: string } }> }
  const artifactNames = run.artifacts.map((item) => item.name)
  expect(artifactNames).toContain('measure_provenance.json')
  expect(artifactNames.some((name) => name.startsWith('measure-001-') && name.endsWith('.zip'))).toBe(true)
  expect(run.patch_journal.map((item) => item.patch.op)).toEqual(['hvac.air_loop.create', 'measure.apply'])
  const verification = await request.post(`/api/runs/${authoredId}/verify`)
  expect(await verification.json()).toMatchObject({ ok: true, status: 'VERIFIED' })
})

test('verified parent run produces immutable EnergyPlus history and comparison', async ({ page }) => {
  test.setTimeout(420_000)
  const pipelineParent = await ensurePipelineModel(page.request)
  await page.route('**/api/capabilities', async (route) => {
    const response = await route.fetch()
    await new Promise((resolve) => setTimeout(resolve, 500))
    await route.fulfill({ response })
  })
  await page.goto('/#/simulation')
  await expect(page.getByRole('heading', { name: /Değiştirilemez simülasyon|Immutable simulation/ })).toBeVisible()
  await page.unroute('**/api/capabilities')
  const setupTrigger = page.getByRole('button', { name: /^(Yeni koşu|New run|Ayarlar|Setup)$/ })
  if (await setupTrigger.getAttribute('aria-expanded') !== 'true') await setupTrigger.click()
  await expect(setupTrigger).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByText('VERIFIED').first()).toBeVisible()
  await expect(page.getByText(/Tam yıl|Full year/)).toBeVisible()
  await expect(page.getByText(/Şartlandırılmış konut alanı|Conditioned residential area/)).toBeVisible()
  await page.locator('.simulation-start-block select').selectOption(pipelineParent.id)

  const recentRuns = page.locator('.recent-run-picker select option')
  const before = await recentRuns.count()
  await page.locator('button.simulation-start').click()
  await expect(page.locator('.simulation-job-control')).toBeVisible()
  await expect(page).toHaveURL(/#\/simulation\?job=[a-f0-9-]+/)
  const attachedJobId = new URL(page.url()).hash.match(/[?&]job=([^&]+)/)?.[1]
  expect(attachedJobId).toBeTruthy()
  await expect(page.getByText(/Geçen süre|Elapsed/)).toBeVisible()
  await expect(page.getByText(/Timeout kalan|Timeout left/)).toBeVisible()
  await expect(page.getByRole('button', { name: 'stdout' })).toBeVisible()
  await expect(page.getByRole('button', { name: /İptal iste|Request cancel/ })).toBeVisible()
  await expect(page.getByText(/Parent doğrulama|Parent verification/)).toBeVisible()

  await page.reload()
  await expect(page).toHaveURL(new RegExp(`job=${attachedJobId}`))
  await expect(page.locator('.simulation-job-control')).toBeVisible()
  await expect(page.getByRole('button', { name: 'stdout' })).toBeVisible()
  const eventIds = await page.locator('.job-event-ledger [data-event-id]').evaluateAll((nodes) => nodes.map((node) => node.getAttribute('data-event-id')))
  expect(new Set(eventIds).size).toBe(eventIds.length)

  const reopened = await page.context().newPage()
  await reopened.goto('/#/simulation')
  await expect(reopened).toHaveURL(new RegExp(`job=${attachedJobId}`))
  await expect(reopened.locator('.simulation-job-control')).toBeVisible()
  await reopened.close()

  await expect(page.locator('.scientific-banner.validated')).toBeVisible({ timeout: 360_000 })
  const energyBasisLabel = page.locator('.energy-basis-strip strong').first()
  await expect(energyBasisLabel).toContainText(/Ideal Loads/)
  expect(await energyBasisLabel.evaluate((element) => element.scrollWidth <= element.clientWidth + 2)).toBe(true)
  await expect(page.locator('.simulation-job-control.terminal')).toBeVisible()
  await page.getByRole('button', { name: 'stderr' }).click()
  await expect(page.locator('.job-log-panel pre')).toBeVisible()
  await expect(page.getByText('11,21').or(page.getByText('11.21')).first()).toBeVisible()
  await expect(page.getByText('16,6').or(page.getByText('16.6')).first()).toBeVisible()
  await page.getByRole('button', { name: /^(Kanıt|Evidence)$/ }).click()
  await expect(page.locator('.qa-count-strip').first()).toContainText('11')
  await page.keyboard.press('Escape')
  await expect(recentRuns).toHaveCount(before + 1)

  const simulations = await page.request.get('/api/simulations').then((response) => response.json()) as Array<{
    id: string
    scenario_name: string
    run_type: 'simulation'
    verification_status: string
    qa: { scientific_status: string }
    result: {
      settings: { parent_model_sha256: string; weather_snapshot_hash: string }
      normalized_energy: { heating_kwh_m2: number; cooling_kwh_m2: number; heating_kwh: number; cooling_kwh: number }
      warnings: { warnings: number }
    }
  }>
  expect(simulations[0].result.normalized_energy.heating_kwh_m2).toBeCloseTo(11.21, 2)
  expect(simulations[0].result.warnings.warnings).toBe(11)
  const packageResponse = await page.request.get(`/api/runs/${simulations[0].id}/export`)
  expect(packageResponse.status()).toBe(200)

  const comparisonClone = JSON.parse(JSON.stringify(simulations[0])) as typeof simulations[number]
  comparisonClone.id = 'e2e-simulation-comparison'
  comparisonClone.scenario_name = 'Immutable comparison fixture'
  await page.route('**/api/runs', async (route) => {
    if (route.request().method() !== 'GET') return route.continue()
    const response = await route.fetch()
    const runs = await response.json() as Array<Record<string, unknown>>
    await route.fulfill({ response, json: [...runs, comparisonClone] })
  })
  await page.route('**/api/simulations/compare*', (route) => {
    const normalized = simulations[0].result.normalized_energy
    const metrics = Object.fromEntries(['heating_kwh_m2', 'cooling_kwh_m2', 'heating_kwh', 'cooling_kwh'].map((key) => {
      const value = normalized[key as keyof typeof normalized]
      return [key, { left: value, right: value, delta: 0, percent: 0 }]
    }))
    return route.fulfill({ json: { left: simulations[0], right: comparisonClone, same_basis: true, metrics } })
  })

  await page.goto(`/#/compare?mode=simulation&left=${simulations[0].id}&right=${comparisonClone.id}`)
  await expect(page.getByTestId('compare-left-run')).toHaveValue(simulations[0].id)
  await expect(page.getByTestId('compare-right-run')).toHaveValue(comparisonClone.id)
  await expect(page.getByText(/Aynı bina, enerji temeli, EPW|Same building, energy basis, EPW/)).toBeVisible()
  await expect(page.getByText('heating_kwh_m2')).toBeVisible()
  await page.goto('/#/runs')
  await page.locator('.run-list > button').filter({ hasText: 'SIMULATION' }).first().click()
  await expect(page.locator('.scientific-banner.validated')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)

  await expect(page.getByRole('heading', { name: /Bina kanıtı ve dosyaları|Building evidence & files/ })).toBeVisible()
})

test('simulation job control requires cancel confirmation and exposes failed-job retry', async ({ page }) => {
  const now = new Date().toISOString()
  const baseJob = {
    kind: 'simulation', refparcela: '4252702YJ2745A', stage: 'EnergyPlus', run_id: null,
    payload: { parent_run_id: 'parent-control' },
    error: null, attempt_count: 1, max_attempts: 2, cancel_requested: 0, timeout_seconds: 600,
    heartbeat_at: now, attempt_started_at: now, terminal_at: null, queue_position: null,
    created_at: now, updated_at: now,
  }
  let creation = 0
  let cancelCalls = 0
  await page.route('**/api/simulations/jobs/active*', (route) => route.fulfill({ json: { job: null } }))
  await page.route('**/api/simulations/eligible-models', (route) => route.fulfill({ json: [{
    id: 'parent-control', refparcela: '4252702YJ2745A', scenario_name: 'Control fixture', created_at: now,
    verification_status: 'VERIFIED', raw_model_sha256: 'a'.repeat(64), canonical_fingerprint: 'b'.repeat(64),
    weather_snapshot_hash: 'c'.repeat(64), settings: {
      run_period: 'annual', timestep_per_hour: 6, output_variables: ['Heating', 'Cooling'],
      energy_output_variables: { heating: 'Heating', cooling: 'Cooling' },
      area_basis: 'conditioned_residential_area', conditioned_residential_area_m2: 2809.9,
    },
  }] }))
  await page.route('**/api/simulations', async (route) => {
    if (route.request().method() !== 'POST') return route.continue()
    creation += 1
    await new Promise((resolve) => setTimeout(resolve, 75))
    if (creation === 1) return route.fulfill({ json: { ...baseJob, id: 'control-running', status: 'running' } })
    return route.fulfill({ json: { ...baseJob, id: 'control-failed', status: 'failed', stage: 'failed', error: 'EnergyPlus diagnostic fixture', terminal_at: now } })
  })
  await page.route('**/api/simulations/jobs/control-running', (route) => route.fulfill({ json: { ...baseJob, id: 'control-running', status: 'running' } }))
  await page.route('**/api/simulations/jobs/control-failed', (route) => route.fulfill({ json: { ...baseJob, id: 'control-failed', status: 'failed', stage: 'failed', error: 'EnergyPlus diagnostic fixture', terminal_at: now } }))
  await page.route('**/api/jobs/control-running/cancel', (route) => {
    cancelCalls += 1
    return route.fulfill({ json: { ...baseJob, id: 'control-running', status: 'canceled', stage: 'canceled', cancel_requested: 1, terminal_at: now } })
  })
  await page.route('**/api/jobs/control-failed/retry', (route) => route.fulfill({ json: { ...baseJob, id: 'control-retry', status: 'queued', stage: 'queued', attempt_count: 0, queue_position: 1 } }))
  await page.route('**/api/simulations/jobs/control-retry', (route) => route.fulfill({ json: { ...baseJob, id: 'control-retry', status: 'queued', stage: 'queued', attempt_count: 0, queue_position: 1 } }))
  await page.route('**/api/jobs/control-*/events*', (route) => route.fulfill({
    contentType: 'text/event-stream',
    body: `id: 1\ndata: ${JSON.stringify({ id: 1, level: 'info', message: 'EnergyPlus', progress: 0.26, created_at: now })}\n\n`,
  }))

  await page.goto('/#/simulation')
  const simulationStart = page.locator('button.simulation-start')
  const newRun = page.getByRole('button', { name: /^(Yeni koşu|New run)$/ })
  await expect(newRun.or(simulationStart)).toBeVisible()
  if (await newRun.isVisible()) await newRun.click()
  await expect(simulationStart).toBeVisible()
  await simulationStart.click()
  await expect(page).toHaveURL(/job=control-running/)
  await page.getByRole('button', { name: /İptal iste|Request cancel/ }).click()
  expect(cancelCalls).toBe(0)
  await expect(page.getByRole('button', { name: /İptali doğrula|Confirm cancel/ })).toBeVisible()
  await page.getByRole('button', { name: /İptali doğrula|Confirm cancel/ }).click()
  expect(cancelCalls).toBe(1)
  await expect(page.getByText(/İptal edildi|Canceled/)).toBeVisible()

  await page.getByRole('button', { name: /^(Ayarlar|Setup|Yeni koşu|New run)$/ }).click()
  await expect(simulationStart).toBeVisible()
  await simulationStart.click()
  await expect(page.getByText('EnergyPlus diagnostic fixture')).toBeVisible()
  await page.getByRole('button', { name: /Yeni iş olarak yeniden dene|Retry as a new job/ }).click()
  await expect(page.locator('.simulation-job-control')).toContainText(/Kuyrukta|Queued/)
  await expect(page.getByText('#1')).toBeVisible()
})

test('Part G creates an immutable model variant and follows its EnergyPlus child', async ({ page }) => {
  const now = new Date().toISOString()
  const parent = {
    id: 'scenario-root', refparcela: '4252702YJ2745A', scenario_name: 'Pilot baseline', created_at: now,
    verification_status: 'VERIFIED', raw_model_sha256: 'a'.repeat(64), canonical_fingerprint: 'b'.repeat(64),
    weather_snapshot_hash: 'c'.repeat(64), weather_source_name: 'ESP_Valencia.082840_IWEC.epw',
    settings: { run_period: 'annual', timestep_per_hour: 6, output_variables: [], energy_output_variables: { heating: 'Heating', cooling: 'Cooling' }, area_basis: 'conditioned_residential_area', conditioned_residential_area_m2: 2809.9 },
  }
  const scenario = { name: 'Comfort +1 / -1 K', heat_delta_c: 1, cool_delta_c: -1, weather_snapshot_hash: parent.weather_snapshot_hash, weather_source_name: parent.weather_source_name, weather_changed: false, reason: 'Comfort sensitivity', source_type: 'human_judgement' }
  const result = {
    schema_version: 1,
    settings: { ...parent.settings, parent_run_id: 'scenario-model', parent_model_sha256: 'd'.repeat(64), parent_canonical_fingerprint: 'e'.repeat(64), weather_snapshot_hash: parent.weather_snapshot_hash, cadastre_heating: { baseline_heating_kwh_m2: 27.97, post_intervention_heating_kwh_m2: 6.63, cooling_reference: null, source_snapshot_hash: 'f'.repeat(64) }, provenance: {}, scenario },
    raw_energy: { area_basis: 'conditioned_residential_area', area_m2: 2809.9, heating: { variable: 'Heating', joule: 125000000000, kwh: 34786, kwh_m2: 12.38 }, cooling: { variable: 'Cooling', joule: 174000000000, kwh: 48386, kwh_m2: 17.22 } },
    normalized_energy: { heating_kwh: 34786, cooling_kwh: 48386, heating_kwh_m2: 12.38, cooling_kwh_m2: 17.22 },
    qa: { scientific_status: 'VALIDATED', all_pass: true, checks: [], warning_summary: { warnings: 11, severes: 0, fatals: 0, categories: [], messages: [] } },
    warnings: { warnings: 11, severes: 0, fatals: 0, categories: [], messages: [] },
    carbon: { s1_scenario: 'S1', s1_consumption_kwh_m2: 32, s1_co2_kg_m2: 6.8, s1_co2_t_yr: 19.1, s2_scenario: 'S2', s2_consumption_kwh_m2: 18, s2_co2_kg_m2: 3.9, s2_co2_t_yr: 10.9 },
    cadastre_heating: { baseline_heating_kwh_m2: 27.97, post_intervention_heating_kwh_m2: 6.63, cooling_reference: null, source_snapshot_hash: 'f'.repeat(64) }, provenance: {},
  }
  const run = { id: 'scenario-simulation-run', job_id: 'scenario-simulation-job', refparcela: parent.refparcela, scenario_name: 'Comfort +1 / -1 K · annual EnergyPlus', config: result.settings, stats: { conditioned_residential_area_m2: 2809.9, warning_count: 11, severe_count: 0, fatal_count: 0 }, qa: result.qa, artifact_dir: '/tmp/scenario', verification_status: 'VERIFIED', run_type: 'simulation', parent_run_id: 'scenario-model', created_at: now, artifacts: [{ name: 'scenario_settings.json', sha256: 'f'.repeat(64), size_bytes: 200 }], result, verification: { ok: true, status: 'VERIFIED', issues: [] }, parent: { id: 'scenario-model', refparcela: parent.refparcela, scenario_name: scenario.name, verification_status: 'VERIFIED' } }
  let scenarioPolls = 0
  let simulationPolls = 0
  let submitted: Record<string, unknown> | null = null
  await page.route('**/api/capabilities', async (route) => {
    const response = await route.fetch()
    const payload = await response.json()
    payload.capabilities.scenario = { part: 'G', runtime_ready: true, declared_ready: true, inspection: { ok: true }, contract: { ok: true, checks: { adapter_hash: true, builder_hash: true, simulation_dependency: true, golden_smoke: true } } }
    await route.fulfill({ response, json: payload })
  })
  await page.route('**/api/simulations/eligible-models', (route) => route.fulfill({ json: [parent] }))
  await page.route('**/api/scenarios/options', (route) => route.fulfill({ json: { ready: true, delta_bounds_c: { min: -3, max: 3, step: 0.1 }, parents: [parent], weather_datasets: [{ id: 'future-epw', name: 'Valencia future', snapshot_hash: '9'.repeat(64), source_name: 'Valencia_2050.epw', managed: true }], source_types: ['human_judgement', 'dataset', 'publication', 'supervisor', 'other'] } }))
  await page.route('**/api/scenarios/jobs/active', (route) => route.fulfill({ json: { job: null } }))
  await page.route('**/api/simulations/jobs/active*', (route) => route.fulfill({ json: { job: null } }))
  await page.route('**/api/scenarios', async (route) => {
    if (route.request().method() !== 'POST') return route.continue()
    submitted = route.request().postDataJSON()
    return route.fulfill({ json: { id: 'scenario-job', kind: 'scenario', status: 'queued', stage: 'queued', refparcela: parent.refparcela, payload: { parent_run_id: parent.id }, attempt_count: 0, max_attempts: 2, cancel_requested: 0, timeout_seconds: 180, queue_position: 1, created_at: now, updated_at: now } })
  })
  await page.route('**/api/scenarios/jobs/scenario-job', (route) => {
    scenarioPolls += 1
    const completed = scenarioPolls > 1
    return route.fulfill({ json: { id: 'scenario-job', kind: 'scenario', status: completed ? 'completed' : 'running', stage: completed ? 'completed' : 'Scenario QA', refparcela: parent.refparcela, run_id: completed ? 'scenario-model' : null, payload: { parent_run_id: parent.id, model_run_id: completed ? 'scenario-model' : null, simulation_job_id: completed ? 'scenario-simulation-job' : null, simulation_error: null }, attempt_count: 1, max_attempts: 2, cancel_requested: 0, timeout_seconds: 180, queue_position: null, heartbeat_at: now, attempt_started_at: now, terminal_at: completed ? now : null, created_at: now, updated_at: now } })
  })
  await page.route('**/api/simulations/jobs/scenario-simulation-job', (route) => {
    simulationPolls += 1
    const completed = simulationPolls > 1
    return route.fulfill({ json: { id: 'scenario-simulation-job', kind: 'simulation', status: completed ? 'completed' : 'running', stage: completed ? 'completed' : 'EnergyPlus', refparcela: parent.refparcela, run_id: completed ? run.id : null, payload: { parent_run_id: 'scenario-model' }, attempt_count: 1, max_attempts: 2, cancel_requested: 0, timeout_seconds: 600, queue_position: null, heartbeat_at: now, attempt_started_at: now, terminal_at: completed ? now : null, created_at: now, updated_at: now } })
  })
  await page.route(/\/api\/simulations$/, (route) => route.fulfill({ json: simulationPolls > 1 ? [run] : [] }))
  await page.route('**/api/jobs/*/events*', (route) => route.fulfill({ contentType: 'text/event-stream', body: `id: 1\ndata: ${JSON.stringify({ id: 1, level: 'info', message: 'Scenario QA', progress: 0.67, created_at: now })}\n\n` }))

  await page.goto('/#/simulation')
  await page.getByRole('button', { name: /Part G senaryosu|Part G scenario/ }).click()
  await expect(page.getByText(/doğrulanmış kök modelden|verified root model/i)).toBeVisible()
  await page.getByLabel(/Senaryo adı|Scenario name/).fill(scenario.name)
  await page.getByLabel(/Isıtma ofseti|Heating offset/).fill('1')
  await page.getByLabel(/Soğutma ofseti|Cooling offset/).fill('-1')
  await page.getByLabel(/Bilimsel gerekçe|Scientific rationale/).fill('Comfort sensitivity')
  await page.getByRole('button', { name: /Varyant oluştur ve simüle et|Create variant and simulate/ }).click()
  expect(submitted).toMatchObject({ parent_run_id: parent.id, heat_delta_c: 1, cool_delta_c: -1, weather_dataset_id: null })
  await expect(page).toHaveURL(/scenario_job=scenario-job/)
  await expect(page).toHaveURL(/job=scenario-simulation-job/, { timeout: 10_000 })
  await expect(page.locator('.part-g-result-strip')).toContainText(scenario.name, { timeout: 10_000 })
  await expect(page.getByTestId('primary-metric-strip')).toContainText(/12[,.]38/)
  await expect(page.getByTestId('primary-metric-strip')).toContainText(/17[,.]22/)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  await expect(page.getByRole('heading', { name: 'Immutable simulation' })).toBeVisible()
})

test('capability gate hides unavailable dependent modules and INVALID results hide interpretation', async ({ page }) => {
  await page.route('**/api/capabilities', async (route) => {
    const response = await route.fetch()
    const payload = await response.json()
    payload.capabilities.simulation.runtime_ready = false
    payload.capabilities.neighborhood.runtime_ready = false
    payload.capabilities.city.runtime_ready = false
    payload.capabilities.lhs.runtime_ready = false
    await route.fulfill({ response, json: payload })
  })
  await page.goto('/#/builder')
  await expect(page.getByRole('link', { name: /Simülasyon|Simulation/ })).toHaveCount(0)
  await expect(page.getByRole('link', { name: /Mahalle|Neighborhood/ })).toHaveCount(0)
  await expect(page.getByRole('link', { name: /Şehir|City/ })).toHaveCount(0)
  await expect(page.getByRole('link', { name: /Belirsizlik|Uncertainty/ })).toHaveCount(0)
  await page.goto('/#/simulation')
  await expect(page.getByRole('heading', { name: /Bina modeli|Building model/ })).toBeVisible()
  await page.goto('/#/neighborhood')
  await expect(page.getByRole('heading', { name: /Veri sağlığı|Data health/ })).toBeVisible()
  await page.goto('/#/city')
  await expect(page.getByRole('heading', { name: /Veri sağlığı|Data health/ })).toBeVisible()
  await page.goto('/#/lhs')
  await expect(page.getByRole('heading', { name: /Bina modeli|Building model/ })).toBeVisible()
  await page.unroute('**/api/capabilities')

  const invalid = {
    id: 'invalid-run', job_id: 'job', refparcela: '4252702YJ2745A', scenario_name: 'Invalid diagnostic',
    config: {}, stats: { warning_count: 1, severe_count: 1, fatal_count: 1 },
    qa: { all_pass: false, scientific_status: 'INVALID', checks: [], warning_summary: { warnings: 1, severes: 1, fatals: 1, categories: [], messages: [] } },
    artifact_dir: '/tmp/invalid', run_type: 'simulation', parent_run_id: 'parent', verification_status: 'VERIFIED',
    raw_model_sha256: 'a'.repeat(64), canonical_fingerprint: 'b'.repeat(64), created_at: new Date().toISOString(), artifacts: [{ name: 'eplusout.err', sha256: 'c'.repeat(64), size_bytes: 20 }],
    verification: { ok: true, status: 'VERIFIED', issues: [] }, parent: { id: 'parent', refparcela: '4252702YJ2745A', scenario_name: 'Parent', verification_status: 'VERIFIED' },
    result: { schema_version: 1, settings: { run_period: 'annual', timestep_per_hour: 6, output_variables: [], energy_output_variables: {}, area_basis: 'conditioned_residential_area', conditioned_residential_area_m2: 2809.9, parent_run_id: 'parent', parent_model_sha256: 'a'.repeat(64), parent_canonical_fingerprint: 'b'.repeat(64), weather_snapshot_hash: 'd'.repeat(64), cadastre_heating: { baseline_heating_kwh_m2: 27.97, post_intervention_heating_kwh_m2: 6.63, cooling_reference: null, source_snapshot_hash: 'e'.repeat(64) }, provenance: {} }, raw_energy: null, normalized_energy: null, qa: { scientific_status: 'INVALID', all_pass: false, checks: [], error_summary: 'Fatal fixture', warning_summary: { warnings: 1, severes: 1, fatals: 1, categories: [], messages: [] } }, warnings: { warnings: 1, severes: 1, fatals: 1, categories: [], messages: [] }, carbon: null, cadastre_heating: { baseline_heating_kwh_m2: 27.97, post_intervention_heating_kwh_m2: 6.63, cooling_reference: null, source_snapshot_hash: 'e'.repeat(64) }, provenance: {} },
  }
  await page.route('**/api/simulations/jobs/active*', (route) => route.fulfill({ json: { job: null } }))
  await page.route('**/api/simulations', (route) => route.fulfill({ json: [invalid] }))
  await page.route('**/api/simulations/eligible-models', (route) => route.fulfill({ json: [] }))
  await page.goto('/?qa=invalid#/simulation')
  await page.locator('.recent-run-picker select').selectOption('invalid-run')
  await expect(page.locator('.scientific-banner.invalid')).toBeVisible()
  await expect(page.locator('.diagnostic-panel')).toBeVisible()
  await expect(page.locator('.energy-hero-band')).toHaveCount(0)
  await expect(page.getByText('27.97')).toHaveCount(0)
  const evidenceFiles = page.getByTestId('energyplus-artifact-files')
  await expect(evidenceFiles).toBeVisible()
  const diagnosticsLink = evidenceFiles.locator('[data-artifact-name="eplusout.err"]')
  await expect(diagnosticsLink).toHaveAttribute('target', '_blank')
  await expect(diagnosticsLink).toHaveAttribute('rel', /noopener/)
  await page.context().route('**/api/runs/invalid-run/artifacts/eplusout.err', (route) => route.fulfill({
    contentType: 'text/plain; charset=utf-8',
    body: 'EnergyPlus diagnostic fixture',
    headers: { 'X-Content-Type-Options': 'nosniff' },
  }))
  const popupPromise = page.waitForEvent('popup')
  await diagnosticsLink.click()
  const artifactPage = await popupPromise
  await artifactPage.waitForLoadState()
  await expect(artifactPage.locator('body')).toContainText('EnergyPlus diagnostic fixture')
  await artifactPage.close()

  await page.route(/\/api\/runs$/, (route) => route.fulfill({ json: [invalid] }))
  await page.route('**/api/simulations/invalid-run', (route) => route.fulfill({ json: invalid }))
  await page.goto('/#/runs?run=invalid-run')
  await expect(page.locator('.run-detail-panel').getByTestId('energyplus-artifact-files')).toBeVisible()
  await expect(page.locator('.run-detail-panel [data-artifact-name="eplusout.err"]')).toHaveAttribute('target', '_blank')
})

test('Data Health explains source drift before offering full baseline revalidation', async ({ page }) => {
  const capabilitiesResponse = await page.request.get('/api/capabilities')
  const driftPayload = await capabilitiesResponse.json()
  driftPayload.capabilities.neighborhood.runtime_ready = false
  driftPayload.capabilities.neighborhood.contract = {
    ok: false,
    checks: {
      fixture_schema: true, adapter_signature: true, adapter_hash: false,
      runner_production: true, runner_hash: false, runner_functions: true,
      simulation_dependency: true, golden_smoke: true,
    },
  }
  driftPayload.capabilities.neighborhood.diagnostic = {
    state: 'source_changed', reason: 'source_changed',
    source: {
      path: '/project/src/neighborhood_pipeline.py',
      current_sha256: 'a'.repeat(64), expected_sha256: 'b'.repeat(64),
      modified_at: new Date().toISOString(),
    },
    latest_verified_evidence: {
      run_id: 'verified-neighborhood', verified_at: new Date().toISOString(),
      source_sha256: 'b'.repeat(64), input_snapshot_hash: 'c'.repeat(64),
    },
    failed_checks: ['adapter_hash', 'runner_hash'],
    features: [
      { key: 'district_scope', available: true, evidence: 'load_stock(district)' },
      { key: 'comfort_scenario', available: true, evidence: 'run_representative(..., scenario)' },
      { key: 'weather_scenario', available: true, evidence: '--epw' },
      { key: 'real_hvac_consumption', available: true, evidence: 'Part C result fields' },
    ],
    revalidation_supported: true,
  }
  await page.route('**/api/capabilities', async (route) => {
    const payload = structuredClone(driftPayload)
    await route.fulfill({ json: payload })
  })
  await page.route('**/api/capabilities/neighborhood/revalidation/plan', (route) => route.fulfill({ json: {
    capability: 'neighborhood', supported: true, eligible: true,
    blocking_checks: [], current_sha256: 'a'.repeat(64), expected_sha256: 'b'.repeat(64),
    duration_seconds: 360, disk_bytes: 2 * 1024 ** 3,
    steps: ['source_snapshot', 'full_baseline', 'scientific_qa', 'golden_comparison', 'hash_promotion'],
    plan_token: 'd'.repeat(64),
  } }))

  await page.goto('/#/health')
  const module = page.locator('[data-capability="neighborhood"]')
  await expect(module).toHaveAttribute('data-capability-state', 'source_changed')
  await module.locator('.capability-summary').click()
  await expect(module).toContainText('aaaaaaaaaaaa')
  await expect(module).toContainText('bbbbbbbbbbbb')
  await expect(module).toContainText(/İlçe kapsamı|District scope/)
  await expect(module).toContainText(/Konfor senaryosu|Comfort scenario/)
  await module.getByRole('button', { name: /Yeniden doğrulamayı incele|Review revalidation/ }).click()
  await expect(module).toContainText(/Tam baseline yeniden doğrulaması|Full baseline revalidation/)
  await expect(module.getByRole('button', { name: /Tam doğrulamayı başlat|Start full validation/ })).toBeEnabled()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
})

test('Part C neighborhood workbench links stock, cluster ledger, immutable totals, and language', async ({ page }) => {
  const now = new Date().toISOString()
  const fingerprint = 'f'.repeat(64)
  const features = {
    type: 'FeatureCollection' as const,
    features: [
      { type: 'Feature' as const, properties: { refparcela: 'A', cluster: 'BlocPluriP04', family: 'BlocPluri', period: 'P04', is_representative: true, heating_kwh_m2: 13.08, cooling_kwh_m2: 16.37 }, geometry: { type: 'Polygon' as const, coordinates: [[[-0.402, 39.492], [-0.397, 39.492], [-0.397, 39.496], [-0.402, 39.496], [-0.402, 39.492]]] } },
      { type: 'Feature' as const, properties: { refparcela: 'B', cluster: 'VivUniP03', family: 'VivUni', period: 'P03', is_representative: true, heating_kwh_m2: 21.4, cooling_kwh_m2: 24.2 }, geometry: { type: 'Polygon' as const, coordinates: [[[-0.396, 39.492], [-0.391, 39.492], [-0.391, 39.496], [-0.396, 39.496], [-0.396, 39.492]]] } },
    ],
  }
  const reps = [
    { cluster: 'BlocPluriP04', family: 'BlocPluri', period: 'P04', refparcela: 'A', n_buildings: 690, rep_area_m2: 562, cluster_med_area_m2: 540, rep_floors: 5, cluster_med_floors: 5, rep_vertices: 4 },
    { cluster: 'VivUniP03', family: 'VivUni', period: 'P03', refparcela: 'B', n_buildings: 269, rep_area_m2: 92, cluster_med_area_m2: 88, rep_floors: 2, cluster_med_floors: 2, rep_vertices: 4 },
  ]
  const clusters = reps.map((item, index) => ({ ...item, qa_all_pass: true, heating_kwh_m2: index ? 21.4 : 13.08, cooling_kwh_m2: index ? 24.2 : 16.37, s1_co2_kg_m2: 5.8, s2_co2_kg_m2: 3.2, eplus_warnings: 11, param_wall_u: 1.33, param_roof_u: 1.92, param_window_u: 5.7, param_window_g: 0.82, param_ground_unconditioned: !index }))
  const mapDescriptor = { fingerprint, sha256: fingerprint, feature_count: 2, bounds: [-0.402, 39.492, -0.391, 39.496], crs: 'EPSG:4326', size_bytes: 1024, url: `/api/neighborhood/maps/${fingerprint}.geojson` }
  const result = {
    schema_version: 1, settings: { scope: 'Benicalap', run_mode: 'full_baseline', method: 'representative_typology_period', capability_version: '1' },
    summary: { scope: 'Benicalap', buildings: 959, clusters_expected: 18, clusters_completed: 18, clusters_failed: 0, qa_passed_clusters: 18, totals: { heating_gwh_yr: 21.04, cooling_gwh_yr: 25.58, s1_co2_t_yr: 11199.67, s2_co2_t_yr: 6176.87, residential_area_m2: 1953038 } },
    qa: { all_pass: true, scientific_status: 'VALIDATED', checks: [], failures: [] }, representatives: reps, clusters, validation: 'Part C validation', map_available: true, map_descriptor: mapDescriptor,
  }
  const run = { id: 'neighborhood-run', job_id: 'neighborhood-job', refparcela: 'BENICALAP', scenario_name: 'Benicalap · full Part C baseline', config: result.settings, stats: result.summary, qa: result.qa, artifact_dir: '/tmp/neighborhood', verification_status: 'VERIFIED', run_type: 'neighborhood', parent_run_id: null, created_at: now, artifacts: [{ name: 'results_buildings.gpkg', sha256: 'a'.repeat(64), size_bytes: 100 }], result, verification: { ok: true, status: 'VERIFIED', issues: [] } }
  await page.route('**/api/capabilities', async (route) => {
    const response = await route.fetch()
    const payload = await response.json()
    payload.capabilities.neighborhood.runtime_ready = true
    payload.capabilities.neighborhood.contract = { ok: true, checks: { fixture_schema: true, adapter_signature: true, adapter_hash: true, runner_production: true, runner_functions: true, simulation_dependency: true, golden_smoke: true } }
    await route.fulfill({ response, json: payload })
  })
  await page.route('**/api/neighborhood/preflight?*', (route) => route.fulfill({ json: { schema_version: 1, scope: 'Benicalap', method: 'representative_typology_period', locked: true, summary: { buildings: 959, clusters: 18, representatives: 18, residential_area_m2: 1953038, imputed_floor_buildings: 51, proxy_area_buildings: 11 }, representatives: reps, map: null, map_descriptor: mapDescriptor, capability: { version: '1', runner_sha256: 'b'.repeat(64), adapter_sha256: 'c'.repeat(64) } } }))
  await page.route('**/api/neighborhood/options', (route) => route.fulfill({ json: stockRunOptions(['BENICALAP', "L'EIXAMPLE"]) }))
  await page.route('**/api/neighborhood/jobs/active', (route) => route.fulfill({ json: { job: null } }))
  let submittedScenario: Record<string, unknown> | null = null
  await page.route(/\/api\/neighborhood\/runs$/, async (route) => {
    if (route.request().method() === 'POST') {
      submittedScenario = route.request().postDataJSON() as Record<string, unknown>
      return route.fulfill({ json: {
        id: 'neighborhood-scenario-job', kind: 'neighborhood', refparcela: "L'EIXAMPLE",
        status: 'queued', stage: 'queued', payload: submittedScenario,
        attempt_count: 0, max_attempts: 2, timeout_seconds: 1800,
      } })
    }
    return route.fulfill({ json: [run] })
  })
  await page.route('**/api/neighborhood/run-summaries', (route) => route.fulfill({ json: [{ id: run.id, scenario_name: run.scenario_name, verification_status: run.verification_status, scientific_status: run.qa.scientific_status, created_at: run.created_at }] }))
  await page.route('**/api/neighborhood/runs/neighborhood-run', (route) => route.fulfill({ json: run }))
  await page.route('**/api/neighborhood/maps/*.geojson', (route) => route.fulfill({ json: features }))

  await page.goto('/#/neighborhood')
  await expect(page.getByRole('heading', { name: /Mahalle ve ilçe stok analizi|Neighborhood and district stock pipeline/ })).toBeVisible()
  await expect(page.locator('.primary-metric-strip > div')).toHaveCount(3)
  const mapRoot = page.locator('.neighborhood-map[data-map-ready="true"]')
  await expect(mapRoot).toBeVisible()
  await expect(mapRoot).toHaveAttribute('data-map-state', 'ready')
  await expect(mapRoot).toHaveAttribute('data-feature-count', '2')
  expect(Number(await mapRoot.getAttribute('data-rendered-feature-count'))).toBeGreaterThan(0)
  const canvas = mapRoot.locator('canvas').first()
  const canvasBox = await canvas.boundingBox()
  expect(canvasBox?.width).toBeGreaterThan(350)
  expect(canvasBox?.height).toBeGreaterThan(300)
  await canvas.click({ position: { x: canvasBox!.width * 0.76, y: canvasBox!.height * 0.5 } })
  await expect(page.locator('.cluster-inspector')).toContainText('VivUniP03')
  await expect(page.getByText('21,04').or(page.getByText('21.04')).first()).toBeVisible()
  await expect(page.getByText('25,58').or(page.getByText('25.58')).first()).toBeVisible()
  await page.locator('.cluster-ledger-list > button').filter({ hasText: 'VivUniP03' }).click()
  await expect(page.locator('.cluster-inspector')).toContainText('VivUniP03')
  await page.getByRole('button', { name: /Isıtma|Heating/ }).click()
  await expect(page.getByRole('button', { name: /Isıtma|Heating/ })).toHaveClass(/active/)
  await page.getByRole('button', { name: /^(Yeni koşu|New run)$/ }).click()
  await expect(page.locator('.contract-pass')).toContainText('7 / 7')
  await page.getByLabel(/Stok kapsamı|Stock scope/).selectOption('building')
  const buildingReference = page.getByLabel(/Kadastro referansı|Cadastral reference/)
  await expect(buildingReference).toBeVisible()
  await expect(page.getByRole('button', { name: /Immutable baseline başlat|Start immutable baseline/ })).toBeDisabled()
  await buildingReference.fill('4252702YJ2745A')
  await expect(page.getByRole('button', { name: /Immutable baseline başlat|Start immutable baseline/ })).toBeEnabled()
  await page.getByLabel(/Stok kapsamı|Stock scope/).selectOption('district')
  await page.locator('.stock-scope-controls select').nth(1).selectOption("L'EIXAMPLE")
  await page.getByRole('button', { name: /^Senaryo$|^Scenario$/ }).click()
  const scenarioStart = page.getByRole('button', { name: /Immutable senaryo başlat|Start immutable scenario/ })
  await expect(scenarioStart).toBeDisabled()
  await page.getByLabel(/Senaryo adı|Scenario name/).fill('District comfort +1 K')
  await page.getByLabel(/Isıtma ofseti|Heating offset/).fill('1')
  await page.getByLabel(/EPW snapshot/).selectOption('future-epw')
  await page.getByLabel(/Bilimsel gerekçe|Scientific rationale/).fill('Supervisor district scenario')
  await page.getByLabel(/Kaynak referansı|Source reference/).fill('meeting-2026-07-16')
  const runPolicy = page.getByTestId('neighborhood-run-policy')
  await runPolicy.getByLabel('Override for this run').check()
  await runPolicy.getByLabel('Ground-floor mode').selectOption('force_conditioned')
  await runPolicy.getByRole('button', { name: 'Review policy' }).click()
  await expect(runPolicy.getByText('forced_ground_mode')).toBeVisible()
  await runPolicy.getByLabel('I confirm this override is resolved only for this new immutable job.').check()
  await expect(scenarioStart).toBeEnabled()
  await scenarioStart.click()
  await expect.poll(() => submittedScenario).not.toBeNull()
  expect(submittedScenario).toMatchObject({
    mode: 'scenario', scope_mode: 'district', district: "L'EIXAMPLE",
    heat_delta_c: 1, cool_delta_c: 0, weather_dataset_id: 'future-epw',
    source_type: 'human_judgement',
    input_policy_override: { ground_floor_mode: 'force_conditioned' },
  })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  await expect(page.getByRole('heading', { name: 'Neighborhood and district stock pipeline' })).toBeVisible()
})

test('Part C invalid cluster result suppresses neighborhood energy interpretation', async ({ page }) => {
  await page.route('**/api/capabilities', async (route) => {
    const response = await route.fetch()
    const payload = await response.json()
    payload.capabilities.neighborhood.runtime_ready = true
    payload.capabilities.neighborhood.contract = { ok: true, checks: { runner_functions: true } }
    await route.fulfill({ response, json: payload })
  })
  const rep = { cluster: 'BlocPluriP04', family: 'BlocPluri', period: 'P04', refparcela: 'A', n_buildings: 690, rep_area_m2: 562, cluster_med_area_m2: 540, rep_floors: 5, cluster_med_floors: 5, rep_vertices: 4 }
  const map = { type: 'FeatureCollection', features: [] }
  const fingerprint = 'e'.repeat(64)
  const mapDescriptor = { fingerprint, sha256: fingerprint, feature_count: 0, bounds: [-0.402, 39.492, -0.391, 39.496], crs: 'EPSG:4326', size_bytes: 32, url: `/api/neighborhood/maps/${fingerprint}.geojson` }
  const invalidResult = { schema_version: 1, settings: { scope: 'Benicalap', run_mode: 'full_baseline', method: 'representative_typology_period', capability_version: '1' }, summary: { scope: 'Benicalap', buildings: 959, clusters_expected: 18, clusters_completed: 17, clusters_failed: 1, qa_passed_clusters: 17, totals: null }, qa: { all_pass: false, scientific_status: 'INVALID', checks: [], failures: [{ cluster: 'BlocPluriP04', refparcela: 'A', stage: 'representative_simulation', error: 'EnergyPlus Severe fixture' }] }, representatives: [rep], clusters: [], validation: '', map_available: false }
  const run = { id: 'invalid-neighborhood', job_id: 'job', refparcela: 'BENICALAP', scenario_name: 'Invalid Part C diagnostic', config: invalidResult.settings, stats: invalidResult.summary, qa: invalidResult.qa, artifact_dir: '/tmp/invalid', verification_status: 'VERIFIED', run_type: 'neighborhood', parent_run_id: null, created_at: new Date().toISOString(), artifacts: [], result: invalidResult, verification: { ok: true, status: 'VERIFIED', issues: [] } }
  await page.route('**/api/neighborhood/preflight?*', (route) => route.fulfill({ json: { schema_version: 1, scope: 'Benicalap', method: 'representative_typology_period', locked: true, summary: { buildings: 959, clusters: 18, representatives: 18, residential_area_m2: 1953038, imputed_floor_buildings: 51, proxy_area_buildings: 11 }, representatives: [rep], map: null, map_descriptor: mapDescriptor, capability: { version: '1', runner_sha256: 'a', adapter_sha256: 'b' } } }))
  await page.route('**/api/neighborhood/options', (route) => route.fulfill({ json: stockRunOptions() }))
  await page.route('**/api/neighborhood/jobs/active', (route) => route.fulfill({ json: { job: null } }))
  await page.route(/\/api\/neighborhood\/runs$/, (route) => route.fulfill({ json: [run] }))
  await page.route('**/api/neighborhood/run-summaries', (route) => route.fulfill({ json: [{ id: run.id, scenario_name: run.scenario_name, verification_status: run.verification_status, scientific_status: run.qa.scientific_status, created_at: run.created_at }] }))
  await page.route('**/api/neighborhood/runs/invalid-neighborhood', (route) => route.fulfill({ json: run }))
  await page.route('**/api/neighborhood/maps/*.geojson', (route) => route.fulfill({ json: map }))
  await page.goto('/#/neighborhood')
  await expect(page.locator('.neighborhood-invalid')).toBeVisible()
  await expect(page.locator('.cluster-failures')).toContainText('EnergyPlus Severe fixture')
  await expect(page.locator('.neighborhood-result-strip')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /Isıtma|Heating/ })).toBeDisabled()
})

test('Part C stock map recovers after its first GeoJSON request fails', async ({ page }) => {
  const fingerprint = 'd'.repeat(64)
  const features = {
    type: 'FeatureCollection' as const,
    features: [{ type: 'Feature' as const, properties: { refparcela: 'A', cluster: 'BlocPluriP04', family: 'BlocPluri', period: 'P04', is_representative: true }, geometry: { type: 'Polygon' as const, coordinates: [[[-0.402, 39.492], [-0.391, 39.492], [-0.391, 39.496], [-0.402, 39.496], [-0.402, 39.492]]] } }],
  }
  const representative = { cluster: 'BlocPluriP04', family: 'BlocPluri', period: 'P04', refparcela: 'A', n_buildings: 959, rep_area_m2: 562, cluster_med_area_m2: 540, rep_floors: 5, cluster_med_floors: 5, rep_vertices: 4 }
  const descriptor = { fingerprint, sha256: fingerprint, feature_count: 1, bounds: [-0.402, 39.492, -0.391, 39.496], crs: 'EPSG:4326', size_bytes: 512, url: `/api/neighborhood/maps/${fingerprint}.geojson` }
  await page.route('**/api/capabilities', async (route) => {
    const response = await route.fetch()
    const payload = await response.json()
    payload.capabilities.neighborhood.runtime_ready = true
    await route.fulfill({ response, json: payload })
  })
  await page.route('**/api/neighborhood/preflight?*', (route) => route.fulfill({ json: { schema_version: 1, scope: 'Benicalap', method: 'representative_typology_period', locked: true, summary: { buildings: 959, clusters: 18, representatives: 18, residential_area_m2: 1953038, imputed_floor_buildings: 51, proxy_area_buildings: 11 }, representatives: [representative], map: null, map_descriptor: descriptor, capability: { version: '1', runner_sha256: 'a', adapter_sha256: 'b' } } }))
  await page.route('**/api/neighborhood/options', (route) => route.fulfill({ json: stockRunOptions() }))
  await page.route('**/api/neighborhood/jobs/active', (route) => route.fulfill({ json: { job: null } }))
  await page.route(/\/api\/neighborhood\/runs$/, (route) => route.fulfill({ json: [] }))
  await page.route('**/api/neighborhood/run-summaries', (route) => route.fulfill({ json: [] }))
  let attempts = 0
  await page.route('**/api/neighborhood/maps/*.geojson', (route) => {
    attempts += 1
    return attempts === 1 ? route.fulfill({ status: 503, json: { detail: 'temporary fixture failure' } }) : route.fulfill({ json: features })
  })

  await page.goto('/#/neighborhood')
  await expect(page.locator('.neighborhood-map-fetch-error')).toBeVisible()
  await page.getByRole('button', { name: /Haritayı yeniden yükle|Reload map/ }).click()
  await expect(page.locator('.neighborhood-map[data-map-state="ready"]')).toBeVisible()
  expect(attempts).toBe(2)
})

test('Part C renders a validated 959-feature stock descriptor within the local budget', async ({ page }, testInfo) => {
  const consoleErrors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  const features = Array.from({ length: 959 }, (_, index) => {
    const column = index % 31
    const row = Math.floor(index / 31)
    const left = -0.414 + column * 0.00042
    const bottom = 39.486 + row * 0.00031
    const right = left + 0.00028
    const top = bottom + 0.00021
    return {
      type: 'Feature' as const,
      properties: {
        refparcela: `BENI-${String(index + 1).padStart(4, '0')}`,
        cluster: index % 2 ? 'BlocPluriP04' : 'VivUniP03',
        family: index % 2 ? 'BlocPluri' : 'VivUni',
        period: index % 2 ? 'P04' : 'P03',
        is_representative: index < 2,
      },
      geometry: {
        type: 'Polygon' as const,
        coordinates: [[[left, bottom], [right, bottom], [right, top], [left, top], [left, bottom]]],
      },
    }
  })
  const collection = { type: 'FeatureCollection' as const, features }
  const representative = { cluster: 'BlocPluriP04', family: 'BlocPluri', period: 'P04', refparcela: 'BENI-0002', n_buildings: 959, rep_area_m2: 562, cluster_med_area_m2: 540, rep_floors: 5, cluster_med_floors: 5, rep_vertices: 4 }
  const fingerprint = '7'.repeat(64)
  const descriptor = { fingerprint, sha256: fingerprint, feature_count: 959, bounds: [-0.414, 39.486, -0.401, 39.496], crs: 'EPSG:4326', size_bytes: 330000, url: `/api/neighborhood/maps/${fingerprint}.geojson` }
  await page.route('**/api/capabilities', async (route) => {
    const response = await route.fetch()
    const payload = await response.json()
    payload.capabilities.neighborhood.runtime_ready = true
    await route.fulfill({ response, json: payload })
  })
  await page.route('**/api/neighborhood/options', (route) => route.fulfill({ json: stockRunOptions() }))
  await page.route('**/api/neighborhood/preflight?*', (route) => route.fulfill({ json: {
    schema_version: 1, scope: 'Benicalap', method: 'representative_typology_period', locked: true,
    summary: { buildings: 959, clusters: 18, representatives: 18, residential_area_m2: 1953038, imputed_floor_buildings: 51, proxy_area_buildings: 11 },
    representatives: [representative], map: null, map_descriptor: descriptor,
    capability: { version: '1', runner_sha256: 'a', adapter_sha256: 'b' },
  } }))
  await page.route('**/api/neighborhood/jobs/active', (route) => route.fulfill({ json: { job: null } }))
  await page.route(/\/api\/neighborhood\/runs$/, (route) => route.fulfill({ json: [] }))
  await page.route('**/api/neighborhood/run-summaries', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/neighborhood/maps/*.geojson', (route) => route.fulfill({ json: collection }))
  const started = Date.now()
  await page.goto('/#/neighborhood')
  const map = page.locator('.neighborhood-map[data-map-state="ready"]')
  await expect(map).toBeVisible({ timeout: 5_000 })
  const readyMs = Date.now() - started
  await expect(map).toHaveAttribute('data-feature-count', '959')
  expect(Number(await map.getAttribute('data-rendered-feature-count'))).toBeGreaterThan(0)
  expect(readyMs).toBeLessThan(5_000)

  const canvas = map.locator('canvas').first()
  const box = await canvas.boundingBox()
  expect(box?.width).toBeGreaterThan(350)
  expect(box?.height).toBeGreaterThan(350)
  const pixels = await canvas.evaluate((element) => {
    const target = element as HTMLCanvasElement
    const gl = target.getContext('webgl2') ?? target.getContext('webgl')
    if (!gl) return { sampled: 0, colored: 0 }
    const buffer = new Uint8Array(target.width * target.height * 4)
    gl.readPixels(0, 0, target.width, target.height, gl.RGBA, gl.UNSIGNED_BYTE, buffer)
    let sampled = 0
    let colored = 0
    for (let index = 0; index < buffer.length; index += 64) {
      sampled += 1
      if (buffer[index] < 220 || buffer[index + 1] < 220 || buffer[index + 2] < 220) colored += 1
    }
    return { sampled, colored }
  })
  expect(pixels.sampled).toBeGreaterThan(1_000)
  expect(pixels.colored / pixels.sampled).toBeGreaterThan(0.01)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  expect(consoleErrors).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('part-c-959-buildings.png'), fullPage: true })
})

test('Part D + F city workbench binds real consumption, demand, 19 districts, and vector tiles', async ({ page }) => {
  const fixture = cityFixture()
  let submittedScenario: Record<string, unknown> | null = null
  let cityPreflightRequests = 0
  const capabilityPayload = await page.request.get('/api/capabilities').then((response) => response.json())
  capabilityPayload.capabilities.city.runtime_ready = true
  capabilityPayload.capabilities.city.contract = { ok: true, checks: { fixture_schema: true, adapter_signature: true, adapter_hash: true, runner_production: true, runner_functions: true, neighborhood_dependency: true, simulation_dependency: true, golden_smoke: true } }
  await page.route('**/api/capabilities', (route) => route.fulfill({ json: capabilityPayload }))
  await page.route('**/api/city/preflight', (route) => {
    cityPreflightRequests += 1
    return route.fulfill({ json: fixture.preflight })
  })
  await page.route('**/api/city/options', (route) => route.fulfill({ json: stockRunOptions() }))
  await page.route('**/api/city/jobs/active', (route) => route.fulfill({ json: { job: null } }))
  await page.route('**/api/city/runs', (route) => {
    if (route.request().method() === 'POST') {
      submittedScenario = route.request().postDataJSON() as Record<string, unknown>
      return route.fulfill({ json: {
        id: 'city-scenario-job', kind: 'city', refparcela: 'VALENCIA', status: 'queued',
        stage: 'queued', payload: submittedScenario, attempt_count: 0, max_attempts: 2,
        timeout_seconds: 1800,
      } })
    }
    return route.fulfill({ json: [fixture.run] })
  })
  await page.route('**/api/city/run-summaries', (route) => route.fulfill({ json: [{ id: fixture.run.id, scenario_name: fixture.run.scenario_name, verification_status: fixture.run.verification_status, scientific_status: fixture.run.qa.scientific_status, created_at: fixture.run.created_at }] }))
  await page.route('**/api/city/jobs/city-scenario-job', (route) => route.fulfill({ json: {
    id: 'city-scenario-job', kind: 'city', refparcela: 'VALENCIA', status: 'completed',
    stage: 'completed', payload: submittedScenario ?? {}, run_id: 'city-run',
    attempt_count: 1, max_attempts: 2, timeout_seconds: 1800,
  } }))
  await page.route('**/api/city/runs/city-run', (route) => route.fulfill({ json: fixture.run }))
  await page.route('**/api/city/runs/city-run/map-metrics', (route) => route.fulfill({ json: {
    schema_version: 1,
    bounds: fixture.preflight.bounds,
    focus_bounds: [-0.42249, 39.43119, -0.31449, 39.53896],
    focus_buildings: 24599,
    focus_coverage: 0.9299,
    clusters: fixture.clusters,
    districts: fixture.districts,
    energy_available: true,
  } }))
  await page.route('**/api/city/runs/city-run/tiles/**', async (route) => {
    const response = await route.fetch({
      url: route.request().url().replace('/runs/city-run/tiles/', '/map/tiles/'),
      maxRetries: 2,
    })
    await route.fulfill({ response })
  })
  await page.route('**/api/runs', (route) => route.fulfill({ json: [fixture.run] }))

  const tileLoaded = page.waitForResponse((response) => response.url().includes('/api/city/runs/city-run/tiles/') && response.status() === 200)
  await page.goto('/#/city')
  await expect(page.getByRole('heading', { name: /Valencia kent stok analizi|Valencia city pipeline/ })).toBeVisible()
  await expect(page.locator('.primary-metric-strip > div')).toHaveCount(3)
  await expect(page.locator('.city-map[data-map-ready="true"]')).toBeVisible()
  const focusExtent = page.getByRole('button', { name: /Kent odağı|Urban focus/ })
  const fullExtent = page.getByRole('button', { name: /Tüm stok|All stock/ })
  await expect(focusExtent).toBeVisible()
  await expect(fullExtent).toBeVisible()
  await expect(page.locator('.city-map')).toHaveAttribute('data-map-zoom', /\d/)
  const focusZoom = Number(await page.locator('.city-map').getAttribute('data-map-zoom'))
  await fullExtent.click()
  await expect.poll(async () => Number(await page.locator('.city-map').getAttribute('data-map-zoom'))).toBeLessThan(focusZoom - 0.5)
  const fullZoom = Number(await page.locator('.city-map').getAttribute('data-map-zoom'))
  await focusExtent.click()
  await expect.poll(async () => Number(await page.locator('.city-map').getAttribute('data-map-zoom'))).toBeGreaterThan(fullZoom + 0.5)
  expect(cityPreflightRequests).toBe(0)
  await page.waitForTimeout(2500)
  await expect(page.locator('.city-map')).toHaveAttribute('data-map-error', '')
  const tileResponse = await tileLoaded
  expect((await tileResponse.body()).byteLength).toBeGreaterThan(0)
  const mapCanvas = page.locator('.city-map canvas').first()
  await expect(mapCanvas).toBeVisible()
  const mapBox = await mapCanvas.boundingBox()
  expect(mapBox?.width).toBeGreaterThan(350)
  expect(mapBox?.height).toBeGreaterThan(350)
  const mapPixels = await mapCanvas.evaluate((element) => {
    const canvas = element as HTMLCanvasElement
    const gl = canvas.getContext('webgl2') ?? canvas.getContext('webgl')
    if (!gl) return { sampled: 0, colored: 0 }
    const pixels = new Uint8Array(canvas.width * canvas.height * 4)
    gl.readPixels(0, 0, canvas.width, canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, pixels)
    let sampled = 0
    let colored = 0
    for (let index = 0; index < pixels.length; index += 64) {
      sampled += 1
      if (pixels[index] < 220 || pixels[index + 1] < 220 || pixels[index + 2] < 220) colored += 1
    }
    return { sampled, colored }
  })
  expect(mapPixels.sampled).toBeGreaterThan(1000)
  expect(mapPixels.colored / mapPixels.sampled).toBeGreaterThan(0.01)
  const zoomIn = page.getByRole('button', { name: 'Zoom in', exact: true })
  let currentZoom = Number(await page.locator('.city-map').getAttribute('data-map-zoom'))
  for (let step = 0; step < 4; step += 1) {
    await zoomIn.click()
    await expect.poll(async () => Number(await page.locator('.city-map').getAttribute('data-map-zoom'))).toBeGreaterThan(currentZoom + 0.75)
    currentZoom = Number(await page.locator('.city-map').getAttribute('data-map-zoom'))
    if (step === 0) await expect(page.locator('.city-map')).toHaveAttribute('data-map-lod', 'building-footprints')
  }
  expect(currentZoom).toBeGreaterThan(14)
  await expect(page.locator('.city-map')).toHaveAttribute('data-map-lod', 'buildings')
  await expect(page.locator('.city-map[data-map-ready="true"]')).toHaveAttribute('data-map-error', '')
  await focusExtent.click()
  await expect(page.getByText('709,28').or(page.getByText('709.28')).first()).toBeVisible()
  await expect(page.getByText('1.992,85').or(page.getByText('1,992.85')).first()).toBeVisible()
  await page.getByRole('button', { name: /^(Tüm metrikler|All metrics)$/ }).click()
  await expect(page.locator('.focused-context-drawer')).toContainText(/596[,.]41/)
  await page.getByRole('button', { name: /^(Seçim|Selection)$/ }).click()
  await expect(page.locator('.cluster-ledger-list > button')).toHaveCount(21)
  await page.getByRole('button', { name: /İlçe|District/, exact: true }).first().click()
  await expect(page.locator('.district-ledger-list > button')).toHaveCount(19)
  await page.locator('.district-ledger-list > button').filter({ hasText: "L'EIXAMPLE" }).click()
  await expect(page.locator('.district-inspector')).toContainText("L'EIXAMPLE")
  await page.getByRole('button', { name: /^(Yeni koşu|New run)$/ }).click()
  await expect.poll(() => cityPreflightRequests).toBe(1)
  await expect(page.locator('.contract-pass')).toContainText('8 / 8')
  await page.getByRole('button', { name: /^Senaryo$|^Scenario$/ }).click()
  await page.getByLabel(/Senaryo adı|Scenario name/).fill('Valencia comfort -1 K')
  await page.getByLabel(/Soğutma ofseti|Cooling offset/).fill('-1')
  await page.getByLabel(/Bilimsel gerekçe|Scientific rationale/).fill('Supervisor city scenario')
  await page.getByLabel(/Kaynak referansı|Source reference/).fill('meeting-2026-07-16')
  await page.getByRole('button', { name: /Immutable senaryo başlat|Start immutable scenario/ }).click()
  await expect.poll(() => submittedScenario).not.toBeNull()
  expect(submittedScenario).toMatchObject({
    mode: 'scenario', heat_delta_c: 0, cool_delta_c: -1,
    weather_dataset_id: null, source_type: 'human_judgement',
  })
  await expect(page.locator('.neighborhood-job-control')).toContainText(/completed/i)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  await expect(page.getByRole('heading', { name: 'Valencia city pipeline' })).toBeVisible()
  await page.getByRole('link', { name: 'View all' }).click()
  await expect(page).toHaveURL(/#\/runs$/)
  await expect(page.getByRole('heading', { name: /Building evidence & files/ })).toBeVisible()
  await expect(page.locator('.run-list > button').filter({ hasText: 'CITY' })).toHaveCount(0)
  await page.unrouteAll({ behavior: 'ignoreErrors' })
})

test('Part D invalid cluster result preserves diagnostics and suppresses city totals', async ({ page }) => {
  const fixture = cityFixture(true)
  await page.route('**/api/capabilities', async (route) => {
    const response = await route.fetch()
    const payload = await response.json()
    payload.capabilities.city.runtime_ready = true
    payload.capabilities.city.contract = { ok: true, checks: { runner_functions: true } }
    await route.fulfill({ response, json: payload })
  })
  await page.route('**/api/city/preflight', (route) => route.fulfill({ json: fixture.preflight }))
  await page.route('**/api/city/options', (route) => route.fulfill({ json: stockRunOptions() }))
  await page.route('**/api/city/jobs/active', (route) => route.fulfill({ json: { job: null } }))
  await page.route('**/api/city/runs', (route) => route.fulfill({ json: [fixture.run] }))
  await page.route('**/api/city/run-summaries', (route) => route.fulfill({ json: [{ id: fixture.run.id, scenario_name: fixture.run.scenario_name, verification_status: fixture.run.verification_status, scientific_status: fixture.run.qa.scientific_status, created_at: fixture.run.created_at }] }))
  await page.route('**/api/city/runs/invalid-city', (route) => route.fulfill({ json: fixture.run }))
  await page.route('**/api/city/runs/invalid-city/map-metrics', (route) => route.fulfill({ json: { schema_version: 1, bounds: fixture.preflight.bounds, clusters: [], districts: fixture.districts, energy_available: false } }))
  await page.goto('/#/city')
  await expect(page.locator('.city-invalid')).toContainText(/enerji|energy/i)
  await expect(page.locator('.cluster-failures')).toContainText('EnergyPlus Severe city fixture')
  await expect(page.locator('.neighborhood-result-strip')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /Isıtma|Heat demand/ })).toBeDisabled()
})

test('historical LHS remains directly reachable but is absent from the single-building navigation', async ({ page }) => {
  const fixture = lhsFixture()
  const onePixelPng = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64')
  await page.route('**/api/capabilities', async (route) => {
    const response = await route.fetch()
    const payload = await response.json()
    payload.capabilities.lhs.runtime_ready = true
    payload.capabilities.lhs.contract = { ok: true, checks: { fixture_schema: true, adapter_signature: true, adapter_hash: true, runner_production: true, runner_hash: true, builder_hash: true, simulation_runner_hash: true, runner_functions: true, simulation_dependency: true, golden_smoke: true } }
    await route.fulfill({ response, json: payload })
  })
  await page.route('**/api/lhs/preflight', (route) => route.fulfill({ json: fixture.preflight }))
  await page.route('**/api/lhs/jobs/active', (route) => route.fulfill({ json: { job: null } }))
  await page.route(/\/api\/lhs\/runs$/, (route) => route.fulfill({ json: fixture.runs }))
  await page.route('**/api/lhs/runs/lhs-run', (route) => route.fulfill({ json: fixture.runs[0] }))
  await page.route('**/api/lhs/runs/lhs-run-previous', (route) => route.fulfill({ json: fixture.runs[1] }))
  await page.route('**/api/lhs/runs/*/figures/*.png', (route) => route.fulfill({ status: 200, contentType: 'image/png', body: onePixelPng }))
  await page.route('**/api/lhs/compare**', (route) => route.fulfill({ json: { left_run_id: 'lhs-run', right_run_id: 'lhs-run-previous', comparable: true, reason: null, rows: [{ output: 'heating_kwh_m2', statistic: 'mean', left: 16.6562, right: 16.6562, delta: 0, percent: 0 }, { output: 'cooling_kwh_m2', statistic: 'mean', left: 14.6308, right: 14.6308, delta: 0, percent: 0 }] } }))
  await page.route(/\/api\/runs$/, (route) => route.fulfill({ json: fixture.runs }))

  await page.goto('/#/lhs')
  await expect(page.getByRole('heading', { name: /LHS belirsizlik çalışma alanı|LHS uncertainty workbench/ })).toBeVisible()
  await expect(page.locator('.nav-group')).toHaveCount(1)
  await expect(page.getByRole('link', { name: /Belirsizlik|Uncertainty/ })).toHaveCount(0)
  const newRunTrigger = page.getByRole('button', { name: /^(Yeni koşu|New run)$/ })
  await newRunTrigger.click()
  await expect(page.locator('#focused-context-drawer')).toHaveCount(1)
  await expect(page.locator('.contract-pass')).toContainText('10 / 10')
  const selectionTrigger = page.getByRole('button', { name: /^(Seçim|Selection)$/ })
  await selectionTrigger.click()
  await expect(page.locator('#focused-context-drawer')).toHaveCount(1)
  await page.keyboard.press('Escape')
  await expect(page.locator('#focused-context-drawer')).toHaveCount(0)
  await expect(selectionTrigger).toBeFocused()
  await selectionTrigger.click()
  await expect(page.locator('.lhs-variable-register > button')).toHaveCount(10)
  await expect(page.locator('.lhs-result-strip')).toContainText(/16[,.]66/)
  await expect(page.locator('.lhs-result-strip')).toContainText(/14[,.]63/)
  await expect(page.locator('.lhs-result-strip')).toContainText(/3[,.]59/)
  await expect(page.locator('.lhs-result-strip > div')).toHaveCount(3)
  const distribution = page.locator('.lhs-figure img')
  await expect(distribution).toBeVisible()
  expect(await distribution.evaluate((image) => (image as HTMLImageElement).naturalWidth)).toBeGreaterThan(0)

  await page.locator('.focused-toolbar-lead .segmented-control button').nth(1).click()
  await expect(page.locator('.lhs-driver-grid section')).toHaveCount(3)
  await expect(page.locator('.lhs-driver-grid')).toContainText(/Hava sızıntısı|Infiltration/)
  await page.locator('.focused-toolbar-lead .segmented-control button').nth(2).click()
  await expect(page.locator('.lhs-sample-ledger tbody tr')).toHaveCount(50)
  await page.locator('.lhs-sample-ledger tbody tr').nth(1).click()
  await expect(page.locator('.lhs-sample-inspector')).toContainText(/ÖRNEK 02|SAMPLE 02/)
  await page.getByRole('button', { name: /^(Kanıt|Evidence)$/ }).click()
  await expect(page.locator('.lhs-qa-workspace .table-head').first()).toContainText('8/8')

  await page.locator('.lhs-compare select').selectOption('lhs-run-previous')
  await expect(page.locator('.lhs-compare-status.pass')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  await expect(page.getByRole('heading', { name: 'LHS uncertainty workbench' })).toBeVisible()
  await page.goto('/#/runs')
  await expect(page.locator('.run-list > button').filter({ hasText: 'LHS' })).toHaveCount(0)
  await expect(page.getByText(/Bu bina için henüz|No model or simulation/)).toBeVisible()
})

test('LHS scientific QA failure preserves evidence but suppresses statistical interpretation', async ({ page }) => {
  const fixture = lhsFixture()
  fixture.result.qa.scientific_status = 'UNVERIFIED'
  fixture.result.qa.all_pass = false
  fixture.result.qa.checks[0].passed = false
  fixture.runs[0].result = fixture.result
  fixture.runs[0].qa = fixture.result.qa
  fixture.runs.splice(1)
  await page.route('**/api/capabilities', async (route) => {
    const response = await route.fetch()
    const payload = await response.json()
    payload.capabilities.lhs.runtime_ready = true
    await route.fulfill({ response, json: payload })
  })
  await page.route('**/api/lhs/preflight', (route) => route.fulfill({ json: fixture.preflight }))
  await page.route('**/api/lhs/jobs/active', (route) => route.fulfill({ json: { job: null } }))
  await page.route(/\/api\/lhs\/runs$/, (route) => route.fulfill({ json: fixture.runs }))

  await page.goto('/#/lhs')
  await expect(page.locator('.scientific-banner.invalid')).toContainText('UNVERIFIED')
  await expect(page.locator('.lhs-result-strip')).toHaveCount(0)
  await expect(page.locator('.focused-toolbar-lead .segmented-control button').nth(0)).toBeDisabled()
  await expect(page.locator('.focused-toolbar-lead .segmented-control button').nth(1)).toBeDisabled()
  await expect(page.locator('.focused-toolbar-lead .segmented-control button').nth(2)).toBeDisabled()
  await expect(page.locator('.lhs-qa-workspace')).toBeVisible()
})

test('stock comparison controls are hidden from the single-building product flow', async ({ page }) => {
  await page.goto('/#/compare?mode=city')
  await expect(page.getByRole('heading', { name: /Aktif bina karşılaştırması|Active-building comparison/ })).toBeVisible()
  await expect(page.getByRole('button', { name: /Mahalle|Neighborhood/ })).toHaveCount(0)
  await expect(page.getByRole('button', { name: /Şehir|City/ })).toHaveCount(0)
  await expect(page.locator('.stock-compare-workspace')).toHaveCount(0)
  await expect(page.locator('.compare-mode-control button')).toHaveCount(2)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
})

test('historical system screens remain directly reachable without entering navigation', async ({ page }) => {
  await page.goto('/#/health')
  await expect(page.getByRole('heading', { name: /Veri sağlığı|Data health/ })).toBeVisible()
  await expect(page.getByTestId('storage-workspace')).toBeVisible()
  await expect(page.getByRole('progressbar', { name: /Disk kullanım oranı|Disk usage/ })).toBeVisible()
  await expect(page.locator('.storage-status')).toHaveText(/READY|WARNING|BLOCKED/)
  await expect(page.getByText(/Güvenli temizleme|Safe cleanup/)).toBeVisible()
  await page.getByRole('button', { name: /Temizliği incele|Review cleanup/ }).click()
  await expect(page.locator('.cleanup-review')).toBeVisible()
  await expect(page.getByText(/immutable koşu|immutable runs/)).toBeVisible()
  await page.getByRole('button', { name: /İptal|Cancel/ }).click()
  await expect(page.locator('.cleanup-review')).toHaveCount(0)
  await page.getByRole('tab', { name: /Veri setleri|Datasets/ }).click()
  await expect(page.getByRole('heading', { name: /GIS alan eşleme|GIS field mapping/ })).toBeVisible()
  await expect(page.locator('.dataset-table').getByText('Valencia city buildings')).toBeVisible()
  await expect(page.getByRole('textbox', { name: /Veri seti adı|Dataset name/ })).toBeVisible()

  await expect(page.getByRole('heading', { name: 'Data health' })).toBeVisible()
  await expect(page.locator('html')).toHaveAttribute('lang', 'en')
  await expect(page.locator('#main-content')).not.toContainText('health.storageCategory.')
  await expect(page.getByTitle('TR / EN')).toHaveCount(0)
  await expect(page.locator('html')).toHaveAttribute('lang', 'en')
  await page.goto('/#/compare')
  await expect(page.getByRole('heading', { name: /Aktif bina karşılaştırması|Active-building comparison/ })).toBeVisible()
  await expect(page.getByText(/Select two different immutable runs\.|Canonical model/)).toBeVisible()
  await expect(page.locator('#main-content')).not.toContainText('[object Object]')
  await expect(page.getByRole('link', { name: 'Batch' })).toHaveCount(0)
  await page.goto('/#/batch')
  await expect(page.getByRole('heading', { name: 'Batch queue' })).toBeVisible()
  await page.getByRole('button', { name: 'Review mapping' }).click()
  await expect(page.getByText('PROFILE CONFIRMATION')).toBeVisible()
  const batchApprovals = page.locator('.batch-geometry-approval input[type="checkbox"]')
  for (let index = 0; index < await batchApprovals.count(); index += 1) await batchApprovals.nth(index).check()
  const [batchResponse] = await Promise.all([
    page.waitForResponse((response) => response.url().endsWith('/api/batches') && response.request().method() === 'POST'),
    page.getByRole('button', { name: 'Confirm batch' }).click(),
  ])
  await expect(page.locator('.batch-card').first()).toBeVisible()
  await expect(page.locator('.batch-card').first().getByRole('progressbar')).toHaveAttribute('aria-valuenow')
  const created = await batchResponse.json() as { job_ids: string[] }
  for (const jobId of created.job_ids) await page.request.post(`/api/jobs/${jobId}/cancel`)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
})

test('Data dictionary and Phase 2 stock policy expose authoritative semantics and guarded defaults', async ({ page }, testInfo) => {
  await page.goto('/#/health')
  await expect(page.locator('html')).toHaveAttribute('lang', 'en')

  const overviewTab = page.getByRole('tab', { name: 'Health & storage' })
  await overviewTab.focus()
  await page.keyboard.press('ArrowRight')
  await expect(page.getByRole('tab', { name: 'Datasets' })).toBeFocused()
  await page.keyboard.press('ArrowRight')
  await expect(page.getByRole('tab', { name: 'Data dictionary' })).toBeFocused()
  await expect(page.getByRole('heading', { name: 'Data dictionary' })).toBeVisible()

  const heating = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'demanda_ca', exact: true }) })
  await expect(heating).toContainText('External-source heating demand (kWh/m²)')
  await expect(heating).toContainText('Meaning confirmed · method open')
  await expect(heating).toContainText('Validation only')

  const consumption = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'ConsumE', exact: true }) })
  await expect(consumption).toContainText('energy-consumption intensity')
  await consumption.getByRole('button', { name: 'ConsumE', exact: true }).click()
  await expect(page.getByText(/Dataset median 47 kWh\/m² · author reference 47 kWh\/m²/)).toBeVisible()
  await expect(page.getByText(/DHW is included and ground floors are excluded/)).toBeVisible()

  const total = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'ConsumETot', exact: true }) })
  await expect(total).toContainText('Source role confirmed · values unusable')
  await expect(total).toContainText(/0%\s*usable/)

  const intervention = page.getByRole('row').filter({ has: page.getByRole('button', { name: 'demanda__1', exact: true }) })
  await expect(intervention).toContainText('Unconfirmed')
  await expect(intervention).toContainText('must not be presented as cooling demand')

  await heating.getByRole('button', { name: 'demanda_ca', exact: true }).click()
  const projectLabel = `UPV validation ${testInfo.project.name}`
  await page.getByRole('textbox', { name: 'Project label' }).fill(projectLabel)
  await page.getByRole('textbox', { name: 'Project note' }).fill('Calculation basis pending confirmation')
  await page.getByRole('button', { name: 'Save note' }).click()
  await expect(page.getByText('Project field note saved.')).toBeVisible()
  await expect(heating).toContainText(projectLabel)

  await expect(page.locator('.companion-coverage').getByText(/Tipo15 dwelling ledger/)).toBeVisible()
  await page.getByRole('button', { name: 'Open Tipo15 dictionary' }).click()
  await expect(page.locator('.dictionary-summary')).toContainText('411,273')
  await expect(page.getByRole('row').filter({ hasText: '442_sup_Residencial' })).toContainText('Scaling')
  expect(await page.locator('.dictionary-table-wrap').evaluate((element) => element.scrollWidth >= element.clientWidth)).toBe(true)

  await page.getByRole('tab', { name: 'Input defaults' }).click()
  await expect(page.getByRole('heading', { name: 'Current input defaults' })).toBeVisible()
  const neighborhoodTab = page.getByRole('tab', { name: 'Neighborhood' })
  await neighborhoodTab.click()
  await expect(page.getByText('Project default policy — configurable; every run resolves an immutable copy.')).toBeVisible()
  await expect(page.locator('.input-contract-status')).toContainText('configurable')
  await expect(page.getByText('Tipo15-derived → family fallback')).toBeVisible()
  await expect(page.getByRole('code').filter({ hasText: 'Tipo15 → cluster-ratio proxy' })).toBeVisible()
  const policy = page.getByTestId('neighborhood-project-policy')
  await expect(policy.getByText('Stock input policy')).toBeVisible()
  await expect(policy.getByLabel('GIS dataset')).toHaveValue('valencia-city')
  await policy.getByRole('button', { name: 'Review policy' }).click()
  await expect(policy.getByText('Runnable')).toBeVisible()
  await expect(policy.getByText('Warnings')).toBeVisible()
  await expect(policy.getByText('Blockers')).toHaveCount(0)
  await policy.getByLabel('Floors field').selectOption('nombre')
  await policy.getByRole('button', { name: 'Review policy' }).click()
  await expect(policy.getByText('Blocked')).toBeVisible()
  await expect(policy.getByText('invalid_numeric_field')).toBeVisible()
  await policy.getByLabel('Floors field').selectOption('altura_max')
  await policy.getByRole('button', { name: 'Review policy' }).click()
  await expect(policy.getByText('Runnable')).toBeVisible()
  await expect(policy.getByLabel('I confirm this changes the project default for future jobs only.')).toBeVisible()
  await neighborhoodTab.focus()
  await page.keyboard.press('ArrowRight')
  await expect(page.getByRole('tab', { name: 'City' })).toBeFocused()
  await expect(page.getByText('Project default policy — configurable; every run resolves an immutable copy.')).toBeVisible()
  await expect(page.locator('.input-contract-status')).toContainText('configurable')
  await expect(page.getByTestId('city-project-policy')).toBeVisible()
  await expect(page.locator('.passive-contract').getByRole('code').filter({ hasText: /^demanda_ca$/ })).toBeVisible()
  await expect(page.locator('.passive-contract').getByRole('code').filter({ hasText: /^ConsumE$/ })).toBeVisible()

  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
})

test('batch empty state explains the first action instead of looking unfinished', async ({ page }) => {
  await page.route('**/api/batches', async (route) => {
    if (route.request().method() === 'GET') return route.fulfill({ json: [] })
    return route.continue()
  })
  await page.goto('/#/batch')
  await expect(page.locator('.batch-empty')).toContainText(/Henüz toplu üretim koşusu yok|No batch runs yet/)
  await expect(page.locator('.batch-empty')).toContainText(/eşlemeyi inceleyerek|review their mapping/i)
  await expect(page.locator('.skeleton-stack')).toHaveCount(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
})

test('historical single-building screens remain callable but absent from product navigation', async ({ page }) => {
  await page.goto('/#/builder')
  await expect(page.getByRole('heading', { name: /Building model/ })).toBeVisible()
  await expect(page.locator('.sidebar .nav-link')).toHaveCount(3)
  await expect(page.getByRole('link', { name: /Building model|Simulation|Compare variants/ })).toHaveCount(0)
  await page.goto('/#/simulation')
  await expect(page.getByRole('heading', { name: /Immutable simulation/i })).toBeVisible()
  await page.goto('/#/compare')
  await expect(page.getByRole('heading', { name: /Active-building comparison/ })).toBeVisible()
})

test('profile and active dataset changes require explicit review', async ({ page }) => {
  await page.goto('/#/builder')
  await page.keyboard.press('Tab')
  await expect(page.locator('.skip-link')).toBeFocused()
  await page.getByRole('button', { name: /Bina|Building/ }).click()
  await page.getByRole('button', { name: /Önerilen TABULA profilini incele|Review suggested TABULA profile/ }).click()
  await expect(page.locator('.profile-confirmation')).toBeVisible()
  await expect(page.getByRole('region', { name: /Profil değişikliğini onayla|Confirm profile change/ })).toBeVisible()
  await expect(page.getByRole('alertdialog')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /Profili uygula|Apply profile/ })).toBeVisible()
  await page.getByRole('button', { name: /İptal|Cancel/ }).click()
  await expect(page.locator('.profile-confirmation')).toHaveCount(0)

  await page.goto('/#/health')
  await page.getByRole('tab', { name: /Veri setleri|Datasets/ }).click()
  const before = await page.request.get('/api/project/settings').then((response) => response.json()) as { building_dataset_id: string }
  const buildingSelect = page.locator('.active-dataset-grid label').filter({ hasText: /Bina GIS|Building GIS/ }).locator('select')
  await buildingSelect.selectOption('')
  await expect(page.locator('.dataset-activation-review')).toBeVisible()
  const unchanged = await page.request.get('/api/project/settings').then((response) => response.json()) as { building_dataset_id: string }
  expect(unchanged.building_dataset_id).toBe(before.building_dataset_id)
  await page.getByRole('button', { name: /Vazgeç|Discard/ }).click()
  await expect(page.locator('.dataset-activation-review')).toHaveCount(0)

})
