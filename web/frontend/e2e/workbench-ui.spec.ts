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

test('persists dark mode across reloads', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '主题设置' }).click()
  await page.getByRole('menuitemradio', { name: '深色' }).click()
  await page.reload()

  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
})

test('expands the compact task log', async ({ page }) => {
  await page.goto('/')

  const panel = page.getByRole('region', { name: '任务日志' })
  await expect(panel).toHaveAttribute('data-collapsed', 'true')
  await page.getByRole('button', { name: '展开任务日志' }).first().click()

  await expect(panel).toHaveAttribute('data-collapsed', 'false')
  await expect(page.getByRole('separator', { name: '调整任务日志高度' })).toBeVisible()
})

test('clamps a persisted log height to the current viewport', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 600 })
  await page.addInitScript(() => {
    localStorage.setItem('designtoolkit-log-collapsed', 'false')
    localStorage.setItem('designtoolkit-log-height', '5000')
  })
  await page.goto('/')

  const panel = page.getByRole('region', { name: '任务日志' })
  await expect(panel).toHaveCSS('height', '270px')
})

for (const viewport of [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'tablet', width: 900, height: 900 },
  { name: 'phone', width: 390, height: 844 },
]) {
  test(`${viewport.name} has no horizontal overflow`, async ({ page }) => {
    await page.setViewportSize(viewport)
    await page.goto('/')

    const dimensions = await page.evaluate(() => ({
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    }))

    expect(dimensions.scrollWidth).toBe(dimensions.clientWidth)
  })
}

test('opens the chroma inspector as a dialog on compact screens', async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 900 })
  await page.goto('/')
  await page.getByRole('button', { name: '色键抠图' }).click()
  await page.getByRole('button', { name: '打开参数检查器' }).click()

  await expect(page.getByRole('dialog', { name: '色键参数' })).toBeVisible()
})

test('does not reopen the chroma inspector after a breakpoint round-trip', async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 900 })
  await page.goto('/')
  await page.getByRole('button', { name: '色键抠图' }).click()
  await page.getByRole('button', { name: '打开参数检查器' }).click()
  await expect(page.getByRole('dialog', { name: '色键参数' })).toBeVisible()

  await page.setViewportSize({ width: 1440, height: 900 })
  await expect(page.getByRole('complementary', { name: '色键参数' })).toBeVisible()
  await page.setViewportSize({ width: 900, height: 900 })

  await expect(page.getByRole('dialog', { name: '色键参数' })).toHaveCount(0)
})

test('keeps the chroma inspector visible beside the canvas on wide screens', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  await page.getByRole('button', { name: '色键抠图' }).click()

  await expect(page.getByRole('complementary', { name: '色键参数' })).toBeVisible()
  await expect(page.getByRole('button', { name: '打开参数检查器' })).toHaveCount(0)
})

test('honors reduced motion preferences', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/')

  const duration = await page.getByRole('button', { name: '主题设置' }).evaluate(
    (element) => Number.parseFloat(getComputedStyle(element).transitionDuration) * 1000,
  )
  expect(duration).toBeLessThanOrEqual(0.01)
})

test('does not emit browser console errors during module navigation', async ({ page }) => {
  const errors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text())
  })

  await page.goto('/')
  await page.getByRole('button', { name: '媒体尺寸修改' }).click()
  await page.getByRole('button', { name: '色键抠图' }).click()

  expect(errors).toEqual([])
})
