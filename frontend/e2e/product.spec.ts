import { expect, test } from '@playwright/test'

test('stock product exposes Files → Run → Outputs with verified evidence', async ({ page }) => {
  const consoleErrors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })

  await page.goto('/#/files')
  await page.waitForLoadState('networkidle')
  await expect(page.getByRole('heading', { name: 'Files' })).toBeVisible()
  // Five inputs since the building-database and microclimate sources landed.
  await expect(page.locator('.file-card')).toHaveCount(5)
  await expect(page.locator('.sidebar .nav-link')).toHaveCount(3)
  await expect(page.locator('.topbar-center')).toContainText('VERIFIED MODEL PROFILE')
  await expect(page.locator('html')).toHaveAttribute('lang', 'en')

  await page.getByRole('link', { name: /Run Execution/ }).click()
  await expect(page.getByRole('heading', { name: 'Run', exact: true })).toBeVisible()
  // The default scope is a reference list and preflight stays disabled until it
  // names a building, so the spec has to make the same choice an operator does.
  await page.locator('.run-field textarea').fill('4252702YJ2745A')
  await page.getByRole('button', { name: 'Run preflight' }).click()
  await expect(page.getByText('PREFLIGHT PASSED')).toBeVisible({ timeout: 60_000 })
  await expect(page.locator('.preflight-result')).toContainText('Runnable')
  await expect(page.getByRole('button', { name: /Start run/ })).toBeEnabled()

  await page.getByRole('link', { name: /Outputs Evidence/ }).click()
  await expect(page.getByRole('heading', { name: 'Outputs' })).toBeVisible()
  await expect(page.getByText('TOTAL SITE ENERGY')).toBeVisible()
  await expect(page.getByText('RESIDENTIAL-AREA EUI', { exact: true })).toBeVisible()
  await expect(page.getByText('conditioned geometry', { exact: true })).toHaveCount(0)

  // Runs are chosen BY NAME, never by position.  This spec used to lean on
  // "the newest run on disk is a city with no Spanish cadastre", which stopped
  // being true the moment a Valencia full-city run finished and sorted first -
  // the second time an ordering assumption here broke on a run nobody added
  // for the test's benefit.
  await page.locator('.output-run-picker select').selectOption('LECCO_1')
  // A city with no Spanish cadastre: `aggregate()` omits both cadastral
  // figures entirely rather than writing zero, so neither the KPI card nor the
  // cluster column may be rendered - an empty accent card reads as a
  // measurement that failed, not as a basis this city does not have.
  await expect(page.getByText('CADASTRAL EUI', { exact: true })).toHaveCount(0)
  await expect(page.locator('.cluster-table thead')).not.toContainText('Cadastral EUI')
  await expect(page.getByText('no cadastral dwelling area')).toBeVisible()
  // The sampling study samples a Valencia pilot building, so it describes
  // nothing on this page.  Rendered unscoped, its band appeared under Lecco's
  // heading beside a different climate, stock and pinned envelope.
  await expect(page.getByText('Uncertainty study (Latin hypercube)')).toHaveCount(0)

  // The same page keeps all of it when the run does carry one.  This half is
  // what makes the half above a condition rather than a deletion, so the two
  // belong in one test.
  await page.locator('.output-run-picker select').selectOption('benicalap_v9')
  await expect(page.getByText('CADASTRAL EUI', { exact: true })).toBeVisible()
  await expect(page.locator('.cluster-table thead')).toContainText('Cadastral EUI')
  await expect(page.getByText('Uncertainty study (Latin hypercube)')).toBeVisible()
  await expect(page.locator('.output-section').filter({ hasText: 'Uncertainty study' }))
    .toContainText('4252702YJ2745A')
  await page.locator('.output-run-picker select').selectOption('LECCO_1')

  await expect(page.getByText('RESULT STATUS', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Full signed ZIP' })).toBeVisible()
  await expect(page.locator('.ledger-table tbody tr').first()).toBeVisible()
  await page.locator('.ledger-table tbody tr').first().click()
  await expect(page.getByText('Signed building package')).toBeVisible()
  await expect(page.locator('.building-evidence-drawer')).not.toContainText('Cluster—')

  await page.getByLabel('Filter ledger by status').selectOption('failed')
  await expect(page.locator('.ledger-table tbody tr')).toHaveCount(1)
  await page.locator('.ledger-table tbody tr').click()
  await expect(page.locator('.building-evidence-drawer')).toContainText('RuntimeError')
  // A building that failed has no energy, so the drawer prints an em dash and
  // never a zero.  The severe count is deliberately not asserted: a failure
  // raised before EnergyPlus ran has none to report, which is a property of the
  // run rather than of this screen.
  await expect(page.locator('.building-evidence-drawer')).toContainText('— kWh/m²')
  // A failed building has no preserved model, so it is not offered a geometry
  // check: the button shares the gate that hides the preserved-files list.
  await expect(page.getByRole('button', { name: /Geometry check/ })).toHaveCount(0)

  // Geometry check rebuilds the scene from the building's own preserved OSM.
  await page.getByLabel('Filter ledger by status').selectOption('ok')
  await expect(page.locator('.ledger-table tbody tr').first().locator('.ledger-status')).toHaveText('ok')
  await page.locator('.ledger-table tbody tr').first().click()

  // The preserved record is offered as a page, not only as raw JSON: of the
  // five preserved files only `eplustbl.htm` ever rendered, so in practice the
  // richest one - `deep_layers.json` - went unread.
  const report = page.getByRole('link', { name: /Building report/ })
  await expect(report).toBeVisible()
  const reportHref = await report.getAttribute('href')
  expect(reportHref).toContain('/report')
  const reportResponse = await page.request.get(reportHref!)
  expect(reportResponse.status()).toBe(200)
  expect(reportResponse.headers()['content-type']).toContain('text/html')
  expect(await reportResponse.text()).toContain('Quality checks')

  // Both leftovers are named, and separately: a failure can be retried into
  // this ledger, an exclusion cannot.
  await expect(page.getByText('Unfinished buildings')).toBeVisible()
  await expect(page.locator('.unfinished-actions')).toContainText('Retry')
  await page.getByRole('button', { name: /Geometry check/ }).click()
  const viewer = page.locator('.geometry-check-drawer [data-render-ready]')
  await expect(viewer).toHaveAttribute('data-render-ready', 'true', { timeout: 60_000 })

  // Selecting another building swaps the scene inside the viewer that is
  // already mounted (the query holds the previous one on screen meanwhile).
  // That is the case the frame counter used to break: `onReady` fires on an
  // exact frame count, so before the `sceneEpoch` remount the overlay stuck on
  // for good after the first scene change and never reported ready again.
  const second = await page.locator('.ledger-table tbody tr').nth(1)
    .locator('td').nth(1).innerText()
  await page.locator('.ledger-table tbody tr').nth(1).click()
  await expect(page.locator('.geometry-check-drawer header strong')).toHaveText(second)
  await expect(viewer).toHaveAttribute('data-render-ready', 'true', { timeout: 60_000 })
  await page.getByRole('button', { name: 'Close geometry check' }).click()
  await expect(page.locator('.geometry-check-drawer')).toHaveCount(0)

  const overflow = await page.evaluate(() => ({
    body: document.body.scrollWidth - document.body.clientWidth,
    main: document.querySelector<HTMLElement>('.app-main')!.scrollWidth - document.querySelector<HTMLElement>('.app-main')!.clientWidth,
  }))
  expect(overflow.body).toBeLessThanOrEqual(0)
  expect(overflow.main).toBeLessThanOrEqual(0)
  expect(consoleErrors).toEqual([])
})
