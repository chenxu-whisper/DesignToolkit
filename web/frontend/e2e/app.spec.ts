import { test, expect } from '@playwright/test'

test('smoke test: navigate and switch modules', async ({ page }) => {
  await page.goto('/')

  await expect(page.getByText('DesignToolkit')).toBeVisible()

  await expect(page.getByText('输入视频路径')).toBeVisible()

  await page.getByRole('button', { name: '媒体尺寸修改' }).click()
  await expect(page.getByText('输入媒体路径')).toBeVisible()

  await page.getByRole('button', { name: '视频转 PNG 序列' }).click()
  await expect(page.getByText('输入视频路径')).toBeVisible()
})

test('opens Studio Chroma Keyer with disabled initial sampling and export controls', async ({ page }) => {
  await page.goto('/')

  await page.getByRole('button', { exact: true, name: 'Studio Chroma Keyer' }).click()

  await expect(page.getByRole('heading', { name: 'Studio Chroma Keyer' })).toBeVisible()
  await expect(page.getByRole('textbox', { name: '输入图片路径' })).toBeVisible()
  await expect(page.getByRole('button', { name: '清除全部采样点' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '导出 PNG' })).toBeDisabled()
})
