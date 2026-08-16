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
  // The newest run on disk is a city with no Spanish cadastre.  `aggregate()`
  // omits both cadastral figures entirely rather than writing zero, so neither
  // the KPI card nor the cluster column may be rendered: an empty accent card
  // reads as a measurement that failed, not as a basis this city does not have.
  await expect(page.getByText('CADASTRAL EUI', { exact: true })).toHaveCount(0)
  await expect(page.locator('.cluster-table thead')).not.toContainText('Cadastral EUI')
  await expect(page.getByText('no cadastral dwelling area')).toBeVisible()

  // The same page keeps both when the run does carry one.  This half is what
  // makes the half above a condition rather than a deletion, so the two belong
  // in one test.
  await page.locator('.output-run-picker select').selectOption('benicalap_v9')
  await expect(page.getByText('CADASTRAL EUI', { exact: true })).toBeVisible()
  await expect(page.locator('.cluster-table thead')).toContainText('Cadastral EUI')
  await page.locator('.output-run-picker select').selectOption({ index: 0 })

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

  const overflow = await page.evaluate(() => ({
    body: document.body.scrollWidth - document.body.clientWidth,
    main: document.querySelector<HTMLElement>('.app-main')!.scrollWidth - document.querySelector<HTMLElement>('.app-main')!.clientWidth,
  }))
  expect(overflow.body).toBeLessThanOrEqual(0)
  expect(overflow.main).toBeLessThanOrEqual(0)
  expect(consoleErrors).toEqual([])
})
