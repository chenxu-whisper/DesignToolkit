import { test, expect } from '@playwright/test'

test('smoke test: navigate and switch modules', async ({ page }) => {
  await page.goto('/')

  await expect(page.getByText('DesignToolkit')).toBeVisible()

  await expect(page.getByText('输入视频路径')).toBeVisible()

  await page.getByRole('button', { name: '媒体尺寸修改' }).click()
  await expect(page.getByText('输入媒体路径')).toBeVisible()

  await page.getByRole('button', { name: '视频转序列' }).click()
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

test('stacks the Chroma editor in the constrained desktop workspace without clipping sample coordinates', async ({ page }) => {
  await page.setViewportSize({ width: 787, height: 900 })
  await page.route('**/api/chroma-keyer/images/inspect', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        input_path: 'E:\\media\\screen.png',
        width: 400,
        height: 200,
        preview_width: 400,
        preview_height: 200,
        preview_url: '/api/chroma-keyer/previews/inspect.svg',
      }),
    })
  })
  await page.route('**/api/chroma-keyer/images/sample', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        rgb: [42, 180, 72],
        hex: '#2AB448',
        source_x: 27,
        source_y: 27,
      }),
    })
  })
  await page.route('**/api/chroma-keyer/previews/inspect.svg', async (route) => {
    await route.fulfill({
      contentType: 'image/svg+xml',
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200"><rect width="400" height="200" fill="#2AB448"/></svg>',
    })
  })

  await page.goto('/')
  await page.getByRole('button', { exact: true, name: 'Studio Chroma Keyer' }).click()
  await page.getByRole('textbox', { name: '输入图片路径' }).fill('E:\\media\\screen.png')
  await page.getByRole('button', { name: '加载图片' }).click()
  await page.getByRole('button', { name: '抠像预览' }).click({ position: { x: 27, y: 27 } })
  await expect(page.locator('.chroma-sample-item span', { hasText: /^27, 27$/ })).toBeVisible()

  const layout = await page.locator('.chroma-editor').evaluate((editor) => {
    const main = editor.querySelector<HTMLElement>('.chroma-editor-main')
    const form = editor.querySelector<HTMLElement>('.chroma-keyer-form')
    const sample = Array.from(editor.querySelectorAll<HTMLElement>('.chroma-sample-item span')).find(
      (element) => element.textContent === '27, 27',
    )
    const sampleItem = sample?.closest<HTMLElement>('.chroma-sample-item')

    if (!main || !form || !sample || !sampleItem) {
      throw new Error('Expected Chroma editor layout and sample coordinate')
    }

    const toBox = (element: HTMLElement) => {
      const { bottom, height, left, right, top, width } = element.getBoundingClientRect()
      return { bottom, height, left, right, top, width }
    }

    return {
      form: toBox(form),
      main: toBox(main),
      sample: toBox(sample),
      sampleItem: toBox(sampleItem),
      sampleClientWidth: sample.clientWidth,
      sampleScrollWidth: sample.scrollWidth,
    }
  })

  expect(layout.main.bottom).toBeLessThanOrEqual(layout.form.top)
  expect(layout.sample.left).toBeGreaterThanOrEqual(layout.sampleItem.left)
  expect(layout.sample.right).toBeLessThanOrEqual(layout.sampleItem.right)
  expect(layout.sampleScrollWidth).toBeLessThanOrEqual(layout.sampleClientWidth)
})
