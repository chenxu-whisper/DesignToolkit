import { expect, test } from '@playwright/test'

test('renders a stable desktop workbench shell', async ({ page }) => {
  await page.goto('/')

  await expect(page.locator('.command-bar')).toHaveCSS('height', '52px')
  await expect(page.locator('.app-shell')).toHaveCSS(
    'grid-template-columns',
    /72px/,
  )
  await expect(page.locator('body')).toHaveCSS('overflow', 'hidden')
})

test('keeps focus visible on keyboard navigation', async ({ page }) => {
  await page.goto('/')
  await page.keyboard.press('Tab')

  const focused = page.locator(':focus-visible')
  await expect(focused).toHaveCount(1)
  await expect(focused).toHaveCSS('outline-style', 'solid')
})
