import { test, expect, type Route } from '@playwright/test'

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

test('contains a loaded Chroma sample in the mobile workspace', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  let inspectRequestCount = 0
  let pickerRequestCount = 0
  let releaseInspectResponse!: () => void
  const inspectResponsePending = new Promise<void>((resolve) => {
    releaseInspectResponse = resolve
  })

  await page.route('**/api/chroma-keyer/images/inspect', async (route) => {
    inspectRequestCount += 1
    await inspectResponsePending
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
  await page.route('**/api/local-paths/pick-files', async (route) => {
    pickerRequestCount += 1
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ cancelled: true, paths: [] }),
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
  const inputPath = page.getByRole('textbox', { name: '输入图片路径' })
  const picker = page.getByRole('button', { name: '指定输入图片路径' })
  await inputPath.fill('E:\\media\\screen.png')
  await inputPath.press('Tab')

  await expect.poll(() => inspectRequestCount).toBe(1)
  await expect(picker).toBeFocused()
  await expect(picker).toHaveAttribute('aria-disabled', 'true')
  await picker.press('Enter')
  expect(inspectRequestCount).toBe(1)
  expect(pickerRequestCount).toBe(0)

  releaseInspectResponse()
  await expect(page.getByRole('button', { name: '抠像预览' })).toBeVisible()
  await page.getByRole('button', { name: '抠像预览' }).click({ position: { x: 27, y: 27 } })
  await expect(page.locator('.chroma-sample-item span', { hasText: /^27, 27$/ })).toBeVisible()

  const layout = await page.locator('.chroma-editor').evaluate((editor) => {
    const main = editor.querySelector<HTMLElement>('.chroma-editor-main')
    const form = editor.querySelector<HTMLElement>('.chroma-keyer-form')
    const sample = Array.from(editor.querySelectorAll<HTMLElement>('.chroma-sample-item span')).find(
      (element) => element.textContent === '27, 27',
    )
    const sampleItem = sample?.closest<HTMLElement>('.chroma-sample-item')
    const deleteButton = sampleItem?.querySelector<HTMLElement>('button')

    if (!main || !form || !sample || !sampleItem || !deleteButton) {
      throw new Error('Expected Chroma editor layout, sample coordinate, and delete button')
    }

    const toBox = (element: HTMLElement) => {
      const { bottom, height, left, right, top, width } = element.getBoundingClientRect()
      return { bottom, height, left, right, top, width }
    }

    return {
      deleteButton: toBox(deleteButton),
      form: toBox(form),
      main: toBox(main),
      sample: toBox(sample),
      sampleItem: toBox(sampleItem),
      sampleItemClientWidth: sampleItem.clientWidth,
      sampleItemScrollWidth: sampleItem.scrollWidth,
      sampleClientWidth: sample.clientWidth,
      sampleScrollWidth: sample.scrollWidth,
      viewport: {
        height: window.innerHeight,
        width: window.innerWidth,
      },
    }
  })

  expect(layout.main.bottom).toBeLessThanOrEqual(layout.form.top)
  expect(layout.sample.left).toBeGreaterThanOrEqual(layout.sampleItem.left)
  expect(layout.sample.right).toBeLessThanOrEqual(layout.sampleItem.right)
  expect(layout.sampleScrollWidth).toBeLessThanOrEqual(layout.sampleClientWidth)
  expect(layout.deleteButton.left).toBeGreaterThanOrEqual(layout.sampleItem.left)
  expect(layout.deleteButton.right).toBeLessThanOrEqual(layout.viewport.width)
  expect(layout.deleteButton.right).toBeLessThanOrEqual(layout.sampleItem.right)
  expect(layout.deleteButton.top).toBeGreaterThanOrEqual(layout.sampleItem.top)
  expect(layout.deleteButton.bottom).toBeLessThanOrEqual(layout.sampleItem.bottom)
  expect(layout.deleteButton.left).toBeGreaterThanOrEqual(0)
  expect(layout.deleteButton.top).toBeGreaterThanOrEqual(0)
  expect(layout.deleteButton.bottom).toBeLessThanOrEqual(layout.viewport.height)
  expect(layout.sampleItemScrollWidth).toBeLessThanOrEqual(layout.sampleItemClientWidth)
})

const chromaParameterNames = [
  'Screen Gain',
  'Screen Balance',
  'Clip Black',
  'Clip White',
  'Shrink/Grow',
  'Feather',
  'Despill Amount',
] as const

const inputPath = 'E:\\media\\screen.png'
const inspectPreviewUrl = '/api/chroma-keyer/previews/inspect.svg'

function createDeferred<Value>() {
  let resolve!: (value: Value) => void
  const promise = new Promise<Value>((promiseResolve) => {
    resolve = promiseResolve
  })
  return { promise, resolve }
}

async function fulfillPreviewSvg(route: Route) {
  const title = new URL(route.request().url()).pathname.split('/').at(-1) ?? 'preview'
  await route.fulfill({
    contentType: 'image/svg+xml',
    body: `<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200"><title>${title}</title><rect width="400" height="200" fill="#2AB448"/></svg>`,
  })
}

test('auto-loads the image selected by the native picker exactly once', async ({ page }) => {
  const inspectBodies: unknown[] = []
  const pickerBodies: unknown[] = []

  await page.route('**/api/local-paths/pick-files', async (route) => {
    pickerBodies.push(route.request().postDataJSON())
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ cancelled: false, paths: [inputPath] }),
    })
  })
  await page.route('**/api/chroma-keyer/images/inspect', async (route) => {
    inspectBodies.push(route.request().postDataJSON())
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        input_path: inputPath,
        width: 400,
        height: 200,
        preview_width: 400,
        preview_height: 200,
        preview_url: inspectPreviewUrl,
      }),
    })
  })
  await page.route('**/api/chroma-keyer/previews/*.svg', fulfillPreviewSvg)

  await page.goto('/')
  await page.getByRole('button', { exact: true, name: 'Studio Chroma Keyer' }).click()
  await expect(page.getByRole('button', { name: '加载图片' })).toHaveCount(0)

  await page.getByRole('button', { name: '指定输入图片路径' }).click()

  await expect(page.getByRole('textbox', { name: '输入图片路径' })).toHaveValue(inputPath)
  await expect(page.getByRole('button', { name: '抠像预览' })).toHaveAttribute('src', inspectPreviewUrl)
  expect(pickerBodies).toEqual([{ multiple: false, file_kind: 'image', title: '选择图片文件' }])
  expect(inspectBodies).toEqual([{ input_path: inputPath }])
})

test('streams live slider previews without blocking controls and keeps the latest response', async ({ page }) => {
  type PreviewBody = {
    input_path: string
    params: {
      clip_black: number
      clip_white: number
      despill_amount: number
      feather: number
      screen_balance: number
      screen_gain: number
      shrink_grow: number
    }
    samples: number[][]
    view_mode: 'alpha' | 'final'
  }
  type CapturedPreview = {
    body: PreviewBody
    fulfilled: Promise<void>
    release: (previewUrl: string) => void
  }

  const previews: CapturedPreview[] = []

  await page.clock.install()
  await page.route('**/api/local-paths/pick-files', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ cancelled: false, paths: [inputPath] }),
    })
  })
  await page.route('**/api/chroma-keyer/images/inspect', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        input_path: inputPath,
        width: 400,
        height: 200,
        preview_width: 400,
        preview_height: 200,
        preview_url: inspectPreviewUrl,
      }),
    })
  })
  await page.route('**/api/chroma-keyer/images/sample', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        rgb: [42, 180, 72],
        hex: '#2AB448',
        source_x: 100,
        source_y: 50,
      }),
    })
  })
  await page.route('**/api/chroma-keyer/preview', async (route) => {
    const response = createDeferred<string>()
    const fulfilled = createDeferred<void>()
    const body = route.request().postDataJSON() as PreviewBody
    previews.push({ body, fulfilled: fulfilled.promise, release: response.resolve })

    if (body.params.screen_gain === 1) {
      response.resolve(`/api/chroma-keyer/previews/${body.view_mode}-${previews.length}.svg`)
    }

    const previewUrl = await response.promise
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ preview_url: previewUrl }),
    })
    fulfilled.resolve(undefined)
  })
  await page.route('**/api/chroma-keyer/previews/*.svg', fulfillPreviewSvg)

  await page.goto('/')
  await page.getByRole('button', { exact: true, name: 'Studio Chroma Keyer' }).click()
  await page.getByRole('button', { name: '指定输入图片路径' }).click()
  const canvas = page.getByRole('button', { name: '抠像预览' })
  await expect(canvas).toHaveAttribute('src', inspectPreviewUrl)
  await canvas.click({ position: { x: 100, y: 50 } })
  await expect(page.getByText('Screen Color', { exact: true })).toBeVisible()

  for (const name of chromaParameterNames) {
    await expect(page.getByRole('slider', { name })).toBeVisible()
  }

  await page.clock.fastForward(150)
  await page.clock.fastForward(150)
  await expect.poll(() => previews.length).toBe(1)
  expect(previews[0].body).toEqual({
    input_path: inputPath,
    params: {
      screen_gain: 1,
      screen_balance: 0.5,
      clip_black: 0,
      clip_white: 1,
      shrink_grow: 0,
      feather: 0,
      despill_amount: 1,
    },
    samples: [[42, 180, 72]],
    view_mode: 'final',
  })
  await expect(canvas).toHaveAttribute('src', '/api/chroma-keyer/previews/final-1.svg')

  await page.getByRole('radio', { name: 'Alpha' }).click()
  await expect.poll(() => previews.length).toBe(2)
  expect(previews[1].body).toEqual({ ...previews[0].body, view_mode: 'alpha' })
  await expect(canvas).toHaveAttribute('src', '/api/chroma-keyer/previews/alpha-2.svg')

  await page.getByRole('radio', { name: 'Final' }).click()
  await expect.poll(() => previews.length).toBe(3)
  expect(previews[2].body).toEqual(previews[0].body)
  await expect(canvas).toHaveAttribute('src', '/api/chroma-keyer/previews/final-3.svg')

  const parameterChanges: ReadonlyArray<{
    key: keyof PreviewBody['params']
    name: (typeof chromaParameterNames)[number]
    value: number
  }> = [
    { key: 'screen_balance', name: 'Screen Balance', value: 0.6 },
    { key: 'clip_black', name: 'Clip Black', value: 0.1 },
    { key: 'clip_white', name: 'Clip White', value: 0.9 },
    { key: 'shrink_grow', name: 'Shrink/Grow', value: 2 },
    { key: 'feather', name: 'Feather', value: 3 },
    { key: 'despill_amount', name: 'Despill Amount', value: 0.8 },
  ]
  let expectedBody = previews[2].body

  for (const change of parameterChanges) {
    const previousCount = previews.length
    await page.getByRole('slider', { name: change.name }).fill(String(change.value))
    await page.clock.fastForward(150)
    await expect.poll(() => previews.length).toBe(previousCount + 1)

    expectedBody = {
      ...expectedBody,
      params: { ...expectedBody.params, [change.key]: change.value },
    }
    expect(previews.at(-1)?.body).toEqual(expectedBody)
  }

  const screenGain = page.getByRole('slider', { name: 'Screen Gain' })
  const previewCountBeforeGain = previews.length
  await screenGain.fill('1.1')
  await page.clock.fastForward(149)
  expect(previews).toHaveLength(previewCountBeforeGain)
  await page.clock.fastForward(1)
  await expect.poll(() => previews.length).toBe(previewCountBeforeGain + 1)
  const olderPreview = previews.at(-1)!

  await screenGain.fill('1.2')
  await page.clock.fastForward(149)
  expect(previews).toHaveLength(previewCountBeforeGain + 1)
  await page.clock.fastForward(1)
  await expect.poll(() => previews.length).toBe(previewCountBeforeGain + 2)
  const latestPreview = previews.at(-1)!

  expect(olderPreview.body).toEqual({
    ...expectedBody,
    params: { ...expectedBody.params, screen_gain: 1.1 },
  })
  expect(latestPreview.body).toEqual({
    ...expectedBody,
    params: { ...expectedBody.params, screen_gain: 1.2 },
  })

  await expect(screenGain).toBeEnabled()
  await expect(page.getByRole('spinbutton', { name: 'Screen Gain 数值' })).toBeEnabled()
  await expect(page.getByRole('radio', { name: 'Alpha' })).toBeEnabled()
  await expect(page.getByRole('button', { name: '删除 Screen Color' })).toBeEnabled()
  await expect(page.getByRole('button', { name: '导出 PNG' })).toBeEnabled()

  latestPreview.release('/api/chroma-keyer/previews/latest.svg')
  await latestPreview.fulfilled
  await expect(canvas).toHaveAttribute('src', '/api/chroma-keyer/previews/latest.svg')

  olderPreview.release('/api/chroma-keyer/previews/older.svg')
  await olderPreview.fulfilled
  await expect(canvas).toHaveAttribute('src', '/api/chroma-keyer/previews/latest.svg')
})

test('contains every slider and value pair at desktop and mobile viewport widths', async ({ page }) => {
  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport)
    await page.goto('/')
    await expect(page.locator('meta[name="viewport"]')).toHaveAttribute(
      'content',
      'width=device-width,initial-scale=1',
    )
    await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN')
    await page.getByRole('button', { exact: true, name: 'Studio Chroma Keyer' }).click()

    const viewportLayout = await page.evaluate(() => ({
      bodyClientWidth: document.body.clientWidth,
      bodyScrollWidth: document.body.scrollWidth,
      documentClientWidth: document.documentElement.clientWidth,
      documentScrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      visualViewportWidth: window.visualViewport?.width ?? null,
    }))
    expect(viewportLayout.documentScrollWidth).toBeLessThanOrEqual(viewportLayout.documentClientWidth)
    expect(viewportLayout.bodyScrollWidth).toBeLessThanOrEqual(viewportLayout.bodyClientWidth)
    expect(viewportLayout.documentClientWidth).toBeLessThanOrEqual(viewportLayout.innerWidth)
    if (viewport.width === 390 && viewportLayout.visualViewportWidth !== null) {
      expect(viewportLayout.visualViewportWidth).toBeCloseTo(390, 0)
    }

    const form = page.locator('.chroma-keyer-form')
    const formBox = await form.boundingBox()
    expect(formBox).not.toBeNull()
    if (!formBox) {
      throw new Error(`Expected Chroma form at ${viewport.width}x${viewport.height}`)
    }

    for (const name of chromaParameterNames) {
      const slider = page.getByRole('slider', { name })
      const value = page.getByRole('spinbutton', { name: `${name} 数值` })
      const field = slider.locator('..')
      const label = field.locator('label', { hasText: name })
      await expect(slider).toBeVisible()
      await expect(value).toBeVisible()

      const [fieldBox, sliderBox, valueBox, labelBox] = await Promise.all([
        field.boundingBox(),
        slider.boundingBox(),
        value.boundingBox(),
        label.boundingBox(),
      ])
      if (!fieldBox || !sliderBox || !valueBox || !labelBox) {
        throw new Error(`Expected ${name} geometry at ${viewport.width}x${viewport.height}`)
      }

      for (const controlBox of [sliderBox, valueBox]) {
        expect(controlBox.x).toBeGreaterThanOrEqual(fieldBox.x - 0.5)
        expect(controlBox.x + controlBox.width).toBeLessThanOrEqual(fieldBox.x + fieldBox.width + 0.5)
        expect(controlBox.x).toBeGreaterThanOrEqual(formBox.x - 0.5)
        expect(controlBox.x + controlBox.width).toBeLessThanOrEqual(formBox.x + formBox.width + 0.5)
      }
      expect(labelBox.x + labelBox.width).toBeLessThanOrEqual(valueBox.x)
    }
  }
})
