import { expect, test } from '@playwright/test'

test('stock product exposes Files → Run → Outputs with verified evidence', async ({ page }) => {
  const consoleErrors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })

  await page.goto('/#/files')
  await page.waitForLoadState('networkidle')
  await expect(page.getByRole('heading', { name: 'Files' })).toBeVisible()
  await expect(page.locator('.file-card')).toHaveCount(4)
  await expect(page.locator('.sidebar .nav-link')).toHaveCount(3)
  await expect(page.locator('.topbar-center')).toContainText('VERIFIED MODEL PROFILE')
  await expect(page.locator('html')).toHaveAttribute('lang', 'en')

  await page.getByRole('link', { name: /Run Execution/ }).click()
  await expect(page.getByRole('heading', { name: 'Run', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Run preflight' }).click()
  await expect(page.getByText('PREFLIGHT PASSED')).toBeVisible({ timeout: 60_000 })
  await expect(page.locator('.preflight-result')).toContainText('Runnable')
  await expect(page.getByRole('button', { name: /Start run/ })).toBeEnabled()

  await page.getByRole('link', { name: /Outputs Evidence/ }).click()
  await expect(page.getByRole('heading', { name: 'Outputs' })).toBeVisible()
  await expect(page.getByText('TOTAL SITE ENERGY')).toBeVisible()
  await expect(page.getByText('RESIDENTIAL-AREA EUI', { exact: true })).toBeVisible()
  await expect(page.getByText('conditioned geometry', { exact: true })).toHaveCount(0)
  await expect(page.getByText('CADASTRAL EUI', { exact: true })).toBeVisible()
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
  await expect(page.locator('.building-evidence-drawer')).toContainText('unexplained Severe')

  const overflow = await page.evaluate(() => ({
    body: document.body.scrollWidth - document.body.clientWidth,
    main: document.querySelector<HTMLElement>('.app-main')!.scrollWidth - document.querySelector<HTMLElement>('.app-main')!.clientWidth,
  }))
  expect(overflow.body).toBeLessThanOrEqual(0)
  expect(overflow.main).toBeLessThanOrEqual(0)
  expect(consoleErrors).toEqual([])
})
