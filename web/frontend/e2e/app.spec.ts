import { test, expect, type Page, type Route } from '@playwright/test'

test('smoke test: navigate and switch modules', async ({ page }) => {
  await page.goto('/')

  await expect(page.getByText('DesignToolkit')).toBeVisible()

  await expect(page.getByText('输入视频路径')).toBeVisible()

  await page.getByRole('button', { name: '媒体尺寸修改' }).click()
  await expect(page.getByText('输入媒体路径')).toBeVisible()

  await page.getByRole('button', { name: '视频转序列' }).click()
  await expect(page.getByText('输入视频路径')).toBeVisible()
})

test('opens 色键抠图 with disabled initial sampling and export controls', async ({ page }) => {
  await page.goto('/')

  await page.getByRole('button', { exact: true, name: '色键抠图' }).click()

  await expect(page.getByRole('heading', { name: '色键抠图' })).toBeVisible()
  await expect(page.getByRole('textbox', { name: '输入图片或视频路径' })).toBeVisible()
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
  await page.getByRole('button', { exact: true, name: '色键抠图' }).click()
  await page.getByRole('button', { name: '打开参数检查器' }).click()
  const inputPath = page.getByRole('textbox', { name: '输入图片或视频路径' })
  const picker = page.getByRole('button', { name: '指定输入图片或视频路径' })
  await inputPath.fill('E:\\media\\screen.png')
  await inputPath.press('Tab')

  await expect.poll(() => inspectRequestCount).toBe(1)
  await expect(picker).toBeFocused()
  await expect(picker).toHaveAttribute('aria-disabled', 'true')
  await picker.press('Enter')
  expect(inspectRequestCount).toBe(1)
  expect(pickerRequestCount).toBe(0)

  releaseInspectResponse()
  await page.getByRole('button', { name: '关闭参数检查器' }).click()
  await expect(page.getByRole('button', { name: '抠像预览' })).toBeVisible()
  await page.getByRole('button', { name: '抠像预览' }).click({ position: { x: 27, y: 27 } })
  await expect(page.locator('.chroma-sample-item span', { hasText: /^27, 27$/ })).toBeVisible()

  const layout = await page.locator('.chroma-editor').evaluate((editor) => {
    const main = editor.querySelector<HTMLElement>('.chroma-editor-main')
    const sample = Array.from(editor.querySelectorAll<HTMLElement>('.chroma-sample-item span')).find(
      (element) => element.textContent === '27, 27',
    )
    const sampleItem = sample?.closest<HTMLElement>('.chroma-sample-item')
    const deleteButton = sampleItem?.querySelector<HTMLElement>('button')

    if (!main || !sample || !sampleItem || !deleteButton) {
      throw new Error('Expected Chroma editor layout, sample coordinate, and delete button')
    }

    const toBox = (element: HTMLElement) => {
      const { bottom, height, left, right, top, width } = element.getBoundingClientRect()
      return { bottom, height, left, right, top, width }
    }

    return {
      deleteButton: toBox(deleteButton),
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

  expect(layout.main.left).toBeGreaterThanOrEqual(0)
  expect(layout.main.right).toBeLessThanOrEqual(layout.viewport.width)
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
  await page.getByRole('button', { exact: true, name: '色键抠图' }).click()
  await expect(page.getByRole('button', { name: '加载图片' })).toHaveCount(0)

  await page.getByRole('button', { name: '指定输入图片或视频路径' }).click()

  await expect(page.getByRole('textbox', { name: '输入图片或视频路径' })).toHaveValue(inputPath)
  await expect(page.getByRole('button', { name: '抠像预览' })).toHaveAttribute('src', inspectPreviewUrl)
  expect(pickerBodies).toEqual([
    {
      allowed_suffixes: ['.mp4', '.webm', '.mov', '.mkv', '.avi', '.m4v', '.png', '.jpg', '.jpeg'],
      multiple: false,
      file_kind: 'media',
      title: '选择图片或视频文件',
    },
  ])
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
  await page.getByRole('button', { exact: true, name: '色键抠图' }).click()
  await page.getByRole('button', { name: '指定输入图片或视频路径' }).click()
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
    await page.getByRole('button', { exact: true, name: '色键抠图' }).click()
    if (viewport.width < 1180) {
      await page.getByRole('button', { name: '打开参数检查器' }).click()
    }

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

type VideoPreviewBody = {
  input_path: string
  timestamp_seconds: number
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

type VideoFrameBody = {
  input_path: string
  timestamp_seconds: number
}

const videoInputPath = 'E:\\media\\moving-screen.mp4'
const videoOutputRoot = 'E:\\exports'
const videoInspectPreviewUrl = '/api/chroma-keyer/previews/video-inspect.svg'

async function installVideoPreviewRoutes(page: Page) {
  await page.route('**/api/chroma-keyer/previews/*.svg', fulfillPreviewSvg)
  await page.route('**/api/chroma-keyer/videos/inspect', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        input_path: videoInputPath,
        width: 400,
        height: 200,
        preview_width: 400,
        preview_height: 200,
        preview_url: videoInspectPreviewUrl,
        duration_seconds: 3,
        fps: 4,
        estimated_frame_count: 12,
        timestamp_seconds: 0,
      }),
    })
  })
}

test('runs the complete chroma video timeline and PNG sequence export workflow', async ({ page }) => {
  await page.clock.install()

  const inspectBodies: unknown[] = []
  const pendingFrameRoutes: Array<{ body: VideoFrameBody; route: Route }> = []
  const sampleBodies: unknown[] = []
  const previewBodies: VideoPreviewBody[] = []
  const pendingPreviewRoutes: Array<{ body: VideoPreviewBody; route: Route }> = []
  const directoryPickerBodies: unknown[] = []
  const taskBodies: Record<string, unknown>[] = []
  let deferNextVideoPreview = false

  const fulfillFrame = async (
    request: { body: VideoFrameBody; route: Route },
    previewName: string,
  ) => {
    await request.route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        preview_url: `/api/chroma-keyer/previews/${previewName}.svg`,
        timestamp_seconds: request.body.timestamp_seconds,
      }),
    })
  }

  const fulfillPreview = async (
    request: { body: VideoPreviewBody; route: Route },
    previewName: string,
  ) => {
    await request.route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        preview_url: `/api/chroma-keyer/previews/${previewName}.svg`,
        timestamp_seconds: request.body.timestamp_seconds,
      }),
    })
  }

  await page.route('**/api/chroma-keyer/previews/*.svg', fulfillPreviewSvg)
  await page.route('**/api/chroma-keyer/videos/inspect', async (route) => {
    inspectBodies.push(route.request().postDataJSON())
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        input_path: videoInputPath,
        width: 400,
        height: 200,
        preview_width: 400,
        preview_height: 200,
        preview_url: videoInspectPreviewUrl,
        duration_seconds: 3,
        fps: 4,
        estimated_frame_count: 12,
        timestamp_seconds: 0,
      }),
    })
  })
  await page.route('**/api/chroma-keyer/videos/frame', async (route) => {
    pendingFrameRoutes.push({
      body: route.request().postDataJSON() as VideoFrameBody,
      route,
    })
  })
  await page.route('**/api/chroma-keyer/videos/sample', async (route) => {
    const body = route.request().postDataJSON() as VideoFrameBody
    sampleBodies.push(body)
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        rgb: [42, 180, 72],
        hex: '#2AB448',
        source_x: 100,
        source_y: 50,
        timestamp_seconds: body.timestamp_seconds,
      }),
    })
  })
  await page.route('**/api/chroma-keyer/videos/preview', async (route) => {
    const body = route.request().postDataJSON() as VideoPreviewBody
    previewBodies.push(body)
    const request = { body, route }
    if (deferNextVideoPreview) {
      deferNextVideoPreview = false
      pendingPreviewRoutes.push(request)
      return
    }
    await fulfillPreview(request, `video-${body.view_mode}-${previewBodies.length}`)
  })
  await page.route('**/api/local-paths/pick-directory', async (route) => {
    directoryPickerBodies.push(route.request().postDataJSON())
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ cancelled: false, paths: [videoOutputRoot] }),
    })
  })
  await page.route('**/api/tasks/chroma-keyer-video', async (route) => {
    taskBodies.push(route.request().postDataJSON() as Record<string, unknown>)
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ task_id: 'video-task-1', status: 'queued' }),
    })
  })
  await page.route('**/api/tasks/video-task-1/events', async (route) => {
    const events = [
      {
        task_id: 'video-task-1',
        timestamp: '2026-09-02T10:00:00Z',
        level: 'progress',
        phase: 'running',
        message: 'Processed video frame 2/4',
        details: { processed_frames: 2, total_frames: 4, progress: 0.5 },
      },
      {
        task_id: 'video-task-1',
        timestamp: '2026-09-02T10:00:01Z',
        level: 'success',
        phase: 'completed',
        message: 'Chroma video key task completed',
        details: { output_path: `${videoOutputRoot}\\moving-screen_keyed_frames`, frame_count: 4 },
      },
    ]
    await route.fulfill({
      contentType: 'text/event-stream',
      body: events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(''),
    })
  })

  await page.goto('/')
  await page.getByRole('button', { exact: true, name: '色键抠图' }).click()
  const input = page.getByRole('textbox', { name: '输入图片或视频路径' })
  await input.fill(videoInputPath)
  await input.press('Enter')

  await expect.poll(() => inspectBodies).toEqual([{ input_path: videoInputPath }])
  const canvas = page.getByRole('button', { name: '抠像预览' })
  await expect(canvas).toHaveAttribute('src', videoInspectPreviewUrl)
  await expect(page.getByRole('slider', { name: '视频时间轴' })).toBeVisible()
  await expect(page.getByText('00:00:00.000 / 00:00:03.000', { exact: true })).toBeVisible()
  await expect(page.getByText('4 FPS')).toBeVisible()
  await expect(page.getByText('400 × 200')).toBeVisible()
  await expect(page.getByText('约 12 帧')).toBeVisible()

  const timeline = page.getByRole('slider', { name: '视频时间轴' })
  await timeline.fill('1')
  await page.clock.fastForward(100)
  await expect.poll(() => pendingFrameRoutes.length).toBe(1)
  await timeline.fill('2')
  await page.clock.fastForward(100)
  await expect.poll(() => pendingFrameRoutes.length).toBe(2)
  expect(pendingFrameRoutes.map(({ body }) => body)).toEqual([
    { input_path: videoInputPath, timestamp_seconds: 1 },
    { input_path: videoInputPath, timestamp_seconds: 2 },
  ])

  await fulfillFrame(pendingFrameRoutes[1], 'video-frame-latest')
  await expect(canvas).toHaveAttribute('src', '/api/chroma-keyer/previews/video-frame-latest.svg')
  await fulfillFrame(pendingFrameRoutes[0], 'video-frame-stale')
  await expect(canvas).toHaveAttribute('src', '/api/chroma-keyer/previews/video-frame-latest.svg')
  await expect(timeline).toHaveValue('2')

  const transport = page.getByRole('group', { name: '视频播放控制' })
  await expect(transport.getByRole('button')).toHaveCount(4)
  for (const name of ['回到开头', '上一帧', '播放', '下一帧']) {
    await expect(transport.getByRole('button', { name })).toBeVisible()
  }

  await transport.getByRole('button', { name: '播放' }).click()
  await expect(transport.getByRole('button', { name: '暂停' })).toBeVisible()
  await expect.poll(() => pendingFrameRoutes.length).toBe(3)
  expect(pendingFrameRoutes[2].body).toEqual({
    input_path: videoInputPath,
    timestamp_seconds: 2,
  })

  await page.clock.fastForward(600)
  await expect(timeline).toHaveValue('2.5')
  expect(pendingFrameRoutes).toHaveLength(3)

  await fulfillFrame(pendingFrameRoutes[2], 'video-playback-slow')
  await expect(canvas).toHaveAttribute('src', '/api/chroma-keyer/previews/video-playback-slow.svg')
  await expect.poll(() => pendingFrameRoutes.length).toBe(4)
  expect(pendingFrameRoutes[3].body).toEqual({
    input_path: videoInputPath,
    timestamp_seconds: 2.5,
  })

  await transport.getByRole('button', { name: '暂停' }).click()
  await expect(transport.getByRole('button', { name: '播放' })).toBeVisible()
  await expect(timeline).toHaveValue('2')
  await expect(canvas).toHaveAttribute('src', '/api/chroma-keyer/previews/video-playback-slow.svg')
  await fulfillFrame(pendingFrameRoutes[3], 'video-playback-stale-after-pause')
  await expect(timeline).toHaveValue('2')
  await expect(canvas).toHaveAttribute('src', '/api/chroma-keyer/previews/video-playback-slow.svg')

  await transport.getByRole('button', { name: '下一帧' }).click()
  await expect.poll(() => pendingFrameRoutes.length).toBe(5)
  expect(pendingFrameRoutes[4].body.timestamp_seconds).toBe(2.25)
  await fulfillFrame(pendingFrameRoutes[4], 'video-step-forward')
  await expect(timeline).toHaveValue('2.25')

  await transport.getByRole('button', { name: '下一帧' }).click()
  await expect.poll(() => pendingFrameRoutes.length).toBe(6)
  expect(pendingFrameRoutes[5].body.timestamp_seconds).toBe(2.5)
  await fulfillFrame(pendingFrameRoutes[5], 'video-step-forward-again')
  await expect(timeline).toHaveValue('2.5')

  await transport.getByRole('button', { name: '下一帧' }).click()
  await expect.poll(() => pendingFrameRoutes.length).toBe(7)
  expect(pendingFrameRoutes[6].body.timestamp_seconds).toBe(2.75)
  await fulfillFrame(pendingFrameRoutes[6], 'video-step-final')
  await expect(timeline).toHaveValue('2.75')

  await transport.getByRole('button', { name: '下一帧' }).click()
  await expect.poll(() => pendingFrameRoutes.length).toBe(8)
  expect(pendingFrameRoutes[7].body.timestamp_seconds).toBe(2.75)
  await fulfillFrame(pendingFrameRoutes[7], 'video-step-forward-clamped')
  await expect(timeline).toHaveValue('2.75')

  await transport.getByRole('button', { name: '上一帧' }).click()
  await expect.poll(() => pendingFrameRoutes.length).toBe(9)
  expect(pendingFrameRoutes[8].body.timestamp_seconds).toBe(2.5)
  await fulfillFrame(pendingFrameRoutes[8], 'video-step-back')
  await expect(timeline).toHaveValue('2.5')

  await transport.getByRole('button', { name: '回到开头' }).click()
  await expect.poll(() => pendingFrameRoutes.length).toBe(10)
  expect(pendingFrameRoutes[9].body.timestamp_seconds).toBe(0)
  await fulfillFrame(pendingFrameRoutes[9], 'video-restart')
  await expect(timeline).toHaveValue('0')

  await transport.getByRole('button', { name: '上一帧' }).click()
  await expect.poll(() => pendingFrameRoutes.length).toBe(11)
  expect(pendingFrameRoutes[10].body.timestamp_seconds).toBe(0)
  await fulfillFrame(pendingFrameRoutes[10], 'video-step-back-clamped')
  await expect(timeline).toHaveValue('0')

  await canvas.click({ position: { x: 100, y: 50 } })
  await expect.poll(() => sampleBodies.length).toBe(1)
  expect(sampleBodies[0]).toEqual({
    input_path: videoInputPath,
    timestamp_seconds: 0,
    x: 100,
    y: 50,
    preview_width: 400,
    preview_height: 200,
  })
  await expect(page.getByText('Screen Color', { exact: true })).toBeVisible()
  const defaultVideoPreviewBody: VideoPreviewBody = {
    input_path: videoInputPath,
    timestamp_seconds: 0,
    samples: [[42, 180, 72]],
    params: {
      screen_gain: 1,
      screen_balance: 0.5,
      clip_black: 0,
      clip_white: 1,
      shrink_grow: 0,
      feather: 0,
      despill_amount: 1,
    },
    view_mode: 'final',
  }
  await expect.poll(() => previewBodies.length).toBe(1)
  expect(previewBodies[0]).toEqual(defaultVideoPreviewBody)

  const firstFinalPreviewUrl = '/api/chroma-keyer/previews/video-final-1.svg'
  await expect(canvas).toHaveAttribute('src', firstFinalPreviewUrl)
  const renderedFinalPreview = await canvas.getAttribute('src')
  if (renderedFinalPreview === null) {
    throw new Error('Expected rendered Final preview URL')
  }
  expect(renderedFinalPreview).toBe(firstFinalPreviewUrl)
  deferNextVideoPreview = true
  await transport.getByRole('button', { name: '播放' }).click()
  await expect(transport.getByRole('button', { name: '暂停' })).toBeVisible()
  await expect.poll(() => previewBodies.length).toBe(2)
  expect(previewBodies[1]).toEqual(defaultVideoPreviewBody)
  await transport.getByRole('button', { name: '暂停' }).click()
  await expect(timeline).toHaveValue('0')
  await expect(canvas).toHaveAttribute('src', renderedFinalPreview)
  await fulfillPreview(pendingPreviewRoutes[0], 'video-final-stale-playback')
  await expect(canvas).toHaveAttribute('src', renderedFinalPreview)
  await expect(timeline).toHaveValue('0')

  await page.getByRole('radio', { name: 'Alpha' }).click()
  await expect.poll(() => previewBodies.length).toBe(3)
  expect(previewBodies[2]).toEqual({ ...defaultVideoPreviewBody, view_mode: 'alpha' })
  await expect(canvas).toHaveAttribute('src', /video-alpha-\d+\.svg/)

  const renderedAlphaPreview = await canvas.getAttribute('src')
  if (renderedAlphaPreview === null) {
    throw new Error('Expected rendered Alpha preview URL')
  }
  expect(renderedAlphaPreview).toMatch(/video-alpha-\d+\.svg/)
  deferNextVideoPreview = true
  await transport.getByRole('button', { name: '播放' }).click()
  await expect.poll(() => previewBodies.length).toBe(4)
  expect(previewBodies[3]).toEqual({ ...defaultVideoPreviewBody, view_mode: 'alpha' })
  await transport.getByRole('button', { name: '暂停' }).click()
  await expect(timeline).toHaveValue('0')
  await fulfillPreview(pendingPreviewRoutes[1], 'video-alpha-stale-playback')
  await expect(canvas).toHaveAttribute('src', renderedAlphaPreview)
  await expect(timeline).toHaveValue('0')

  await page.getByRole('radio', { name: 'Final' }).click()
  await expect.poll(() => previewBodies.length).toBe(5)
  expect(previewBodies[4]).toEqual(defaultVideoPreviewBody)
  await expect(canvas).toHaveAttribute('src', /video-final-\d+\.svg/)

  const parameterChanges: ReadonlyArray<{
    key: keyof VideoPreviewBody['params']
    name: (typeof chromaParameterNames)[number]
    numberValue: number
    sliderValue: number
  }> = [
    { key: 'screen_gain', name: 'Screen Gain', sliderValue: 1.1, numberValue: 1.2 },
    { key: 'screen_balance', name: 'Screen Balance', sliderValue: 0.55, numberValue: 0.6 },
    { key: 'clip_black', name: 'Clip Black', sliderValue: 0.05, numberValue: 0.1 },
    { key: 'clip_white', name: 'Clip White', sliderValue: 0.95, numberValue: 0.9 },
    { key: 'shrink_grow', name: 'Shrink/Grow', sliderValue: 1, numberValue: 2 },
    { key: 'feather', name: 'Feather', sliderValue: 1, numberValue: 2 },
    { key: 'despill_amount', name: 'Despill Amount', sliderValue: 0.9, numberValue: 0.8 },
  ]
  let expectedPreviewBody = previewBodies[4]
  for (const change of parameterChanges) {
    const previousCount = previewBodies.length
    const slider = page.getByRole('slider', { name: change.name })
    const number = page.getByRole('spinbutton', { name: `${change.name} 数值` })
    await slider.fill(String(change.sliderValue))
    await expect(number).toHaveValue(String(change.sliderValue))
    await number.fill(String(change.numberValue))
    await expect(slider).toHaveValue(String(change.numberValue))
    expect(previewBodies).toHaveLength(previousCount)
    expectedPreviewBody = {
      ...expectedPreviewBody,
      params: { ...expectedPreviewBody.params, [change.key]: change.numberValue },
    }
    await page.clock.fastForward(150)
    await expect.poll(() => previewBodies.length).toBe(previousCount + 1)
    expect(previewBodies.slice(previousCount)).toEqual([expectedPreviewBody])
  }

  const previewCountBeforeEndPlayback = previewBodies.length
  deferNextVideoPreview = true
  await transport.getByRole('button', { name: '播放' }).click()
  await expect.poll(() => previewBodies.length).toBe(previewCountBeforeEndPlayback + 1)
  expect(previewBodies.at(-1)).toEqual(expectedPreviewBody)
  await page.clock.fastForward(3000)
  expect(previewBodies).toHaveLength(previewCountBeforeEndPlayback + 1)

  await fulfillPreview(pendingPreviewRoutes.at(-1)!, 'video-keyed-playback-zero')
  await expect.poll(() => previewBodies.length).toBe(previewCountBeforeEndPlayback + 2)
  expect(previewBodies.at(-1)).toEqual({
    ...expectedPreviewBody,
    timestamp_seconds: 2.75,
  })
  await expect(transport.getByRole('button', { name: '播放' })).toBeVisible()
  await expect(timeline).toHaveValue('2.75')
  await expect(canvas).toHaveAttribute(
    'src',
    `/api/chroma-keyer/previews/video-final-${previewCountBeforeEndPlayback + 2}.svg`,
  )

  deferNextVideoPreview = true
  await transport.getByRole('button', { name: '播放' }).click()
  await expect.poll(() => previewBodies.length).toBe(previewCountBeforeEndPlayback + 3)
  expect(previewBodies.at(-1)).toEqual({
    ...expectedPreviewBody,
    timestamp_seconds: 0,
  })
  await expect(transport.getByRole('button', { name: '暂停' })).toBeVisible()
  await transport.getByRole('button', { name: '暂停' }).click()
  await expect(timeline).toHaveValue('2.75')
  await fulfillPreview(pendingPreviewRoutes.at(-1)!, 'video-keyed-replay-stale')
  await expect(timeline).toHaveValue('2.75')
  await expect(canvas).toHaveAttribute(
    'src',
    `/api/chroma-keyer/previews/video-final-${previewCountBeforeEndPlayback + 2}.svg`,
  )

  await page.getByRole('button', { name: '指定输出根目录（可选）' }).click()
  await expect(page.getByRole('textbox', { name: '输出根目录（可选）' })).toHaveValue(videoOutputRoot)
  expect(directoryPickerBodies).toEqual([{ title: '选择 PNG 序列输出根目录' }])

  await page.getByRole('button', { name: '导出 PNG 序列' }).click()
  await expect.poll(() => taskBodies.length).toBe(1)
  expect(taskBodies[0]).toEqual({
    input_path: videoInputPath,
    output_root: videoOutputRoot,
    samples: [[42, 180, 72]],
    params: {
      screen_gain: 1.2,
      screen_balance: 0.6,
      clip_black: 0.1,
      clip_white: 0.9,
      shrink_grow: 2,
      feather: 2,
      despill_amount: 0.8,
    },
  })
  expect(taskBodies[0]).not.toHaveProperty('output_path')
  await page.getByRole('button', { name: '展开任务日志' }).first().click()
  await expect(page.getByText('Processed video frame 2/4')).toBeVisible()
  await expect(page.getByLabel('任务状态：完成')).toBeVisible()
  await expect(page.getByText(`输出：${videoOutputRoot}\\moving-screen_keyed_frames`)).toBeVisible()
})

test('keeps the loaded video editor within desktop and 390px mobile layouts', async ({ page }) => {
  await installVideoPreviewRoutes(page)

  for (const viewport of [
    { width: 1026, height: 900 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport)
    await page.goto('/')
    await page.getByRole('button', { exact: true, name: '色键抠图' }).click()
    await page.getByRole('button', { name: '打开参数检查器' }).click()
    const input = page.getByRole('textbox', { name: '输入图片或视频路径' })
    await input.fill(videoInputPath)
    await input.press('Enter')
    await expect(page.getByRole('slider', { name: '视频时间轴' })).toHaveCount(1)

    const inspectorLayout = await page.locator('.chroma-keyer-form').evaluate((form) => {
      const parameterHeaders = Array.from(
        form.querySelectorAll<HTMLElement>('.chroma-parameter-header'),
      )
      const toBox = (element: HTMLElement) => {
        const { bottom, height, left, right, top, width } = element.getBoundingClientRect()
        return { bottom, height, left, right, top, width }
      }
      return {
        form: toBox(form as HTMLElement),
        parameters: parameterHeaders.map((header) => {
          const field = header.closest<HTMLElement>('.chroma-parameter-field')
          const label = header.querySelector<HTMLElement>('.field-label')
          const number = header.querySelector<HTMLElement>('.chroma-parameter-number')
          if (!field || !label || !number) {
            throw new Error('Expected Chroma parameter label and numeric input')
          }
          const style = getComputedStyle(label)
          return {
            card: toBox(field),
            header: toBox(header),
            label: toBox(label),
            labelClientHeight: label.clientHeight,
            labelClientWidth: label.clientWidth,
            labelScrollHeight: label.scrollHeight,
            labelScrollWidth: label.scrollWidth,
            name: label.textContent?.trim() ?? '',
            number: toBox(number),
            overflowWrap: style.overflowWrap,
            textOverflow: style.textOverflow,
            whiteSpace: style.whiteSpace,
          }
        }),
        viewportWidth: window.innerWidth,
      }
    })

    await page.getByRole('button', { name: '关闭参数检查器' }).click()
    await expect(page.getByRole('slider', { name: '视频时间轴' })).toBeVisible()

    const layout = await page.locator('.chroma-editor').evaluate((editor) => {
      const main = editor.querySelector<HTMLElement>('.chroma-editor-main')
      const timeline = editor.querySelector<HTMLElement>('.chroma-video-timeline')
      const controls = editor.querySelector<HTMLElement>('.chroma-video-timeline-controls')
      const transport = editor.querySelector<HTMLElement>('.chroma-video-transport')
      const range = editor.querySelector<HTMLElement>('.chroma-video-timeline-range')
      const timecode = editor.querySelector<HTMLElement>('.chroma-video-timecode')
      const metadata = editor.querySelector<HTMLElement>('.chroma-video-metadata')
      const transportButtons = Array.from(
        transport?.querySelectorAll<HTMLElement>('button') ?? [],
      )
      if (
        !main
        || !timeline
        || !controls
        || !transport
        || !range
        || !timecode
        || !metadata
        || transportButtons.length !== 4
      ) {
        throw new Error('Expected loaded video editor geometry')
      }
      const toBox = (element: HTMLElement) => {
        const { bottom, height, left, right, top, width } = element.getBoundingClientRect()
        return { bottom, height, left, right, top, width }
      }
      return {
        bodyClientWidth: document.body.clientWidth,
        bodyScrollWidth: document.body.scrollWidth,
        buttons: transportButtons.map(toBox),
        controls: toBox(controls),
        documentClientWidth: document.documentElement.clientWidth,
        documentScrollWidth: document.documentElement.scrollWidth,
        main: toBox(main),
        metadata: toBox(metadata),
        range: toBox(range),
        timecode: toBox(timecode),
        timeline: toBox(timeline),
        transport: toBox(transport),
      }
    })

    const expectContained = (
      inner: { bottom: number; left: number; right: number; top: number },
      outer: { bottom: number; left: number; right: number; top: number },
    ) => {
      expect(inner.left).toBeGreaterThanOrEqual(outer.left - 0.5)
      expect(inner.right).toBeLessThanOrEqual(outer.right + 0.5)
      expect(inner.top).toBeGreaterThanOrEqual(outer.top - 0.5)
      expect(inner.bottom).toBeLessThanOrEqual(outer.bottom + 0.5)
    }
    const expectNoOverlap = (
      first: { bottom: number; left: number; right: number; top: number },
      second: { bottom: number; left: number; right: number; top: number },
    ) => {
      const overlaps = (
        first.left < second.right - 0.5
        && first.right > second.left + 0.5
        && first.top < second.bottom - 0.5
        && first.bottom > second.top + 0.5
      )
      expect(overlaps).toBe(false)
    }

    expect(layout.documentScrollWidth).toBeLessThanOrEqual(layout.documentClientWidth)
    expect(layout.bodyScrollWidth).toBeLessThanOrEqual(layout.bodyClientWidth)
    expect(layout.timeline.left).toBeGreaterThanOrEqual(layout.main.left - 0.5)
    expect(layout.timeline.right).toBeLessThanOrEqual(layout.main.right + 0.5)
    expectContained(layout.controls, layout.main)
    expectContained(layout.transport, layout.main)
    expect(layout.buttons).toHaveLength(4)
    for (const button of layout.buttons) {
      expectContained(button, layout.transport)
      expect(button.width).toBeCloseTo(32, 0)
      expect(button.height).toBeCloseTo(32, 0)
      expectNoOverlap(button, layout.range)
      expectNoOverlap(button, layout.timecode)
    }
    expect(layout.metadata.left).toBeGreaterThanOrEqual(layout.main.left - 0.5)
    expect(layout.metadata.right).toBeLessThanOrEqual(layout.main.right + 0.5)
    expect(inspectorLayout.form.left).toBeGreaterThanOrEqual(0)
    expect(inspectorLayout.form.right).toBeLessThanOrEqual(inspectorLayout.viewportWidth)
    expect(inspectorLayout.parameters.map(({ name }) => name)).toEqual(chromaParameterNames)
    for (const parameter of inspectorLayout.parameters) {
      expect(parameter.labelScrollHeight).toBeLessThanOrEqual(parameter.labelClientHeight + 1)
      expect(parameter.labelScrollWidth).toBeLessThanOrEqual(parameter.labelClientWidth + 1)
      expect(parameter.overflowWrap).toBe('normal')
      expect(parameter.textOverflow).not.toBe('ellipsis')
      expect(parameter.whiteSpace).not.toBe('nowrap')
      expect(parameter.header.height).toBeCloseTo(34, 0)
      expect(parameter.number.width).toBeCloseTo(68, 0)
      expect(parameter.number.left).toBeGreaterThanOrEqual(parameter.header.left - 0.5)
      expect(
        parameter.number.right,
        `${parameter.name} numeric input must stay within its header at ${viewport.width}px`,
      ).toBeLessThanOrEqual(parameter.header.right + 0.5)
      expect(parameter.number.top).toBeGreaterThanOrEqual(parameter.header.top - 0.5)
      expect(parameter.number.bottom).toBeLessThanOrEqual(parameter.header.bottom + 0.5)
      expect(parameter.number.left).toBeGreaterThanOrEqual(parameter.card.left - 0.5)
      expect(parameter.number.right).toBeLessThanOrEqual(parameter.card.right + 0.5)
      expect(parameter.number.top).toBeGreaterThanOrEqual(parameter.card.top - 0.5)
      expect(parameter.number.bottom).toBeLessThanOrEqual(parameter.card.bottom + 0.5)
      expect(parameter.label.right).toBeLessThanOrEqual(parameter.number.left + 0.5)
      expect(parameter.label.left).toBeGreaterThanOrEqual(parameter.header.left - 0.5)
      expect(parameter.label.right).toBeLessThanOrEqual(parameter.header.right + 0.5)
      expect(parameter.label.top).toBeGreaterThanOrEqual(parameter.header.top - 0.5)
      expect(parameter.label.bottom).toBeLessThanOrEqual(parameter.header.bottom + 0.5)
      expect(parameter.label.left).toBeGreaterThanOrEqual(parameter.card.left - 0.5)
      expect(parameter.label.right).toBeLessThanOrEqual(parameter.card.right + 0.5)
      expect(parameter.label.top).toBeGreaterThanOrEqual(parameter.card.top - 0.5)
      expect(parameter.label.bottom).toBeLessThanOrEqual(parameter.card.bottom + 0.5)
    }
  }
})
