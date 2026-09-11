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

const chromaZoomImagePath = 'E:\\media\\zoom-source.png'
const chromaZoomVideoPath = 'E:\\media\\zoom-source.mp4'
const chromaZoomImageWidth = 800
const chromaZoomImageHeight = 400

type ImageSampleBody = {
  input_path: string
  preview_height: number
  preview_width: number
  x: number
  y: number
}

type ChromaZoomRouteState = {
  frameRequests: unknown[]
  imageSampleRequests: ImageSampleBody[]
  videoPreviewRequests: unknown[]
  videoSampleRequests: unknown[]
}

async function installChromaZoomRoutes(page: Page): Promise<ChromaZoomRouteState> {
  const state: ChromaZoomRouteState = {
    frameRequests: [],
    imageSampleRequests: [],
    videoPreviewRequests: [],
    videoSampleRequests: [],
  }

  await page.route('**/api/chroma-keyer/previews/*.svg', async (route) => {
    const title = new URL(route.request().url()).pathname.split('/').at(-1) ?? 'zoom-preview'
    await route.fulfill({
      contentType: 'image/svg+xml',
      body: [
        `<svg xmlns="http://www.w3.org/2000/svg" width="${chromaZoomImageWidth}" height="${chromaZoomImageHeight}">`,
        `<title>${title}</title>`,
        `<rect width="${chromaZoomImageWidth}" height="${chromaZoomImageHeight}" fill="#2a6f55"/>`,
        '<circle cx="400" cy="200" r="40" fill="#d9efe6"/>',
        '</svg>',
      ].join(''),
    })
  })
  await page.route('**/api/chroma-keyer/images/inspect', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        input_path: chromaZoomImagePath,
        width: chromaZoomImageWidth,
        height: chromaZoomImageHeight,
        preview_width: 960,
        preview_height: 480,
        preview_url: '/api/chroma-keyer/previews/zoom-image.svg',
      }),
    })
  })
  await page.route('**/api/chroma-keyer/images/sample', async (route) => {
    const body = route.request().postDataJSON() as ImageSampleBody
    state.imageSampleRequests.push(body)
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        rgb: [42, 180, 72],
        hex: '#2AB448',
        source_x: Math.min(
          Math.floor(body.x * chromaZoomImageWidth / body.preview_width),
          chromaZoomImageWidth - 1,
        ),
        source_y: Math.min(
          Math.floor(body.y * chromaZoomImageHeight / body.preview_height),
          chromaZoomImageHeight - 1,
        ),
      }),
    })
  })
  await page.route('**/api/chroma-keyer/preview', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        preview_url: `/api/chroma-keyer/previews/zoom-image-keyed-${state.imageSampleRequests.length}.svg`,
      }),
    })
  })
  await page.route('**/api/chroma-keyer/videos/inspect', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        input_path: chromaZoomVideoPath,
        width: 1280,
        height: 720,
        preview_width: 960,
        preview_height: 540,
        preview_url: '/api/chroma-keyer/previews/zoom-video.svg',
        duration_seconds: 4,
        fps: 4,
        estimated_frame_count: 16,
        timestamp_seconds: 0,
      }),
    })
  })
  await page.route('**/api/chroma-keyer/videos/frame', async (route) => {
    const body = route.request().postDataJSON() as { timestamp_seconds: number }
    state.frameRequests.push(body)
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        preview_url: `/api/chroma-keyer/previews/zoom-video-frame-${state.frameRequests.length}.svg`,
        timestamp_seconds: body.timestamp_seconds,
      }),
    })
  })
  await page.route('**/api/chroma-keyer/videos/sample', async (route) => {
    const body = route.request().postDataJSON() as { timestamp_seconds: number }
    state.videoSampleRequests.push(body)
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        rgb: [42, 180, 72],
        hex: '#2AB448',
        source_x: 600,
        source_y: 300,
        timestamp_seconds: body.timestamp_seconds,
      }),
    })
  })
  await page.route('**/api/chroma-keyer/videos/preview', async (route) => {
    const body = route.request().postDataJSON() as { timestamp_seconds: number }
    state.videoPreviewRequests.push(body)
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        preview_url: `/api/chroma-keyer/previews/zoom-video-keyed-${state.videoPreviewRequests.length}.svg`,
        timestamp_seconds: body.timestamp_seconds,
      }),
    })
  })

  return state
}

async function loadChromaZoomMedia(page: Page, path: string) {
  await page.goto('/')
  await page.getByRole('button', { exact: true, name: '色键抠图' }).click()

  const inspectorTrigger = page.getByRole('button', { name: '打开参数检查器' })
  if (await inspectorTrigger.isVisible()) {
    await inspectorTrigger.click()
  }

  const input = page.getByRole('textbox', { name: '输入图片或视频路径' })
  await input.fill(path)
  await input.press('Enter')
  await expect(page.getByRole('button', { name: '抠像预览' })).toBeVisible()

  const inspectorClose = page.getByRole('button', { name: '关闭参数检查器' })
  if (await inspectorClose.isVisible()) {
    await inspectorClose.click()
  }
}

async function requireBox(
  locator: ReturnType<Page['locator']>,
  label: string,
) {
  const box = await locator.boundingBox()
  expect(box, `${label} must have a bounding box`).not.toBeNull()
  return box!
}

test('Chroma preview zoom loads stable image geometry and supports toolbar commands', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await installChromaZoomRoutes(page)
  await loadChromaZoomMedia(page, chromaZoomImagePath)

  const viewport = page.locator('.chroma-preview-viewport')
  const canvas = page.locator('.chroma-preview-canvas')
  const image = page.locator('.chroma-preview-image')
  const toolbar = page.getByRole('toolbar', { name: '预览缩放控制' })
  const status = toolbar.getByRole('status')

  for (const [label, locator] of [
    ['viewport', viewport],
    ['canvas', canvas],
    ['image', image],
  ] as const) {
    const box = await requireBox(locator, label)
    expect(box.width, `${label} width`).toBeGreaterThan(0)
    expect(box.height, `${label} height`).toBeGreaterThan(0)
  }
  await expect(toolbar).toBeVisible()

  const fittedStatus = await status.textContent()
  await toolbar.getByRole('button', { name: '实际大小' }).click()
  await expect(status).toHaveText('100%')
  await toolbar.getByRole('button', { name: '放大预览' }).click()
  await expect(status).toHaveText('125%')
  await toolbar.getByRole('button', { name: '缩小预览' }).click()
  await expect(status).toHaveText('100%')
  await toolbar.getByRole('button', { name: '适应窗口' }).click()
  await expect(status).toHaveText(fittedStatus ?? '')
})

test('Chroma preview zoom keeps wheel anchors stable and separates pan from sampling', async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 800 })
  const routes = await installChromaZoomRoutes(page)
  await loadChromaZoomMedia(page, chromaZoomImagePath)

  const viewport = page.locator('.chroma-preview-viewport')
  const canvas = page.locator('.chroma-preview-canvas')
  const image = page.locator('.chroma-preview-image')
  const toolbar = page.getByRole('toolbar', { name: '预览缩放控制' })
  await toolbar.getByRole('button', { name: '实际大小' }).click()
  await expect.poll(async () => {
    const box = await requireBox(image, 'actual-size image')
    return box.width
  }).toBeCloseTo(chromaZoomImageWidth, 0)

  const canvasBox = await requireBox(canvas, 'canvas')
  const anchor = {
    x: canvasBox.x + canvasBox.width * 0.38,
    y: canvasBox.y + canvasBox.height * 0.62,
  }
  const beforeWheel = await requireBox(image, 'image before wheel zoom')
  const sourceAtAnchor = {
    x: ((anchor.x - beforeWheel.x) / beforeWheel.width) * chromaZoomImageWidth,
    y: ((anchor.y - beforeWheel.y) / beforeWheel.height) * chromaZoomImageHeight,
  }

  await page.mouse.move(anchor.x, anchor.y)
  await page.mouse.wheel(0, -240)
  await expect(toolbar.getByRole('status')).not.toHaveText('100%')
  await expect.poll(async () => {
    const afterWheel = await requireBox(image, 'image after wheel zoom')
    const nextSource = {
      x: ((anchor.x - afterWheel.x) / afterWheel.width) * chromaZoomImageWidth,
      y: ((anchor.y - afterWheel.y) / afterWheel.height) * chromaZoomImageHeight,
    }
    return Math.max(
      Math.abs(nextSource.x - sourceAtAnchor.x),
      Math.abs(nextSource.y - sourceAtAnchor.y),
    )
  }).toBeLessThanOrEqual(2)

  await viewport.focus()
  await page.keyboard.down('Space')
  await expect(viewport).toHaveClass(/is-pan-ready/)
  await page.mouse.move(anchor.x, anchor.y)
  await page.mouse.down()
  await page.mouse.move(anchor.x + 48, anchor.y + 24, { steps: 4 })
  await expect(viewport).toHaveClass(/is-panning/)
  await page.mouse.up()
  await page.keyboard.up('Space')
  expect(routes.imageSampleRequests).toHaveLength(0)

  const clickBox = await requireBox(canvas, 'canvas after pan')
  const imageAtClick = await requireBox(image, 'image at sample click')
  const clickPoint = {
    x: clickBox.x + clickBox.width / 2,
    y: clickBox.y + clickBox.height / 2,
  }
  await page.mouse.click(clickPoint.x, clickPoint.y)
  await expect.poll(() => routes.imageSampleRequests.length).toBe(1)

  const sampleRequest = routes.imageSampleRequests[0]
  expect(sampleRequest.input_path).toBe(chromaZoomImagePath)
  expect(sampleRequest.x).toBe(Math.floor(clickPoint.x - imageAtClick.x))
  expect(sampleRequest.y).toBe(Math.floor(clickPoint.y - imageAtClick.y))
  expect(sampleRequest.preview_width).toBeCloseTo(imageAtClick.width, 3)
  expect(sampleRequest.preview_height).toBeCloseTo(imageAtClick.height, 3)
  const returnedSource = {
    x: Math.min(
      Math.floor(sampleRequest.x * chromaZoomImageWidth / sampleRequest.preview_width),
      chromaZoomImageWidth - 1,
    ),
    y: Math.min(
      Math.floor(sampleRequest.y * chromaZoomImageHeight / sampleRequest.preview_height),
      chromaZoomImageHeight - 1,
    ),
  }

  const marker = page.getByLabel('采样点 1')
  await expect(marker).toBeVisible()
  await expect.poll(async () => {
    const imageBox = await requireBox(image, 'sampled image')
    const markerBox = await requireBox(marker, 'sample marker')
    const expected = {
      x: imageBox.x + (returnedSource.x / chromaZoomImageWidth) * imageBox.width,
      y: imageBox.y + (returnedSource.y / chromaZoomImageHeight) * imageBox.height,
    }
    const actual = {
      x: markerBox.x + markerBox.width / 2,
      y: markerBox.y + markerBox.height / 2,
    }
    return Math.max(Math.abs(actual.x - expected.x), Math.abs(actual.y - expected.y))
  }).toBeLessThanOrEqual(2)
})

test('Chroma preview toolbar has coarse-pointer targets without mobile overflow', async ({ browser }) => {
  const context = await browser.newContext({
    hasTouch: true,
    isMobile: true,
    viewport: { width: 390, height: 844 },
  })
  const mobilePage = await context.newPage()
  const pageErrors: string[] = []
  const consoleErrors: string[] = []
  mobilePage.on('pageerror', (error) => pageErrors.push(error.message))
  mobilePage.on('console', (message) => {
    if (message.type() === 'error') {
      consoleErrors.push(message.text())
    }
  })

  try {
    await installChromaZoomRoutes(mobilePage)
    await loadChromaZoomMedia(mobilePage, chromaZoomImagePath)

    const viewport = await requireBox(
      mobilePage.locator('.chroma-preview-viewport'),
      'mobile preview viewport',
    )
    const canvas = await requireBox(
      mobilePage.locator('.chroma-preview-canvas'),
      'mobile preview canvas',
    )
    const toolbarLocator = mobilePage.getByRole('toolbar', { name: '预览缩放控制' })
    const toolbar = await requireBox(toolbarLocator, 'mobile preview toolbar')
    const buttonBoxes = await toolbarLocator.getByRole('button').evaluateAll((buttons) => (
      buttons.map((button) => {
        const { height, width } = button.getBoundingClientRect()
        return { height, width }
      })
    ))
    const widths = await mobilePage.evaluate(() => ({
      bodyClient: document.body.clientWidth,
      bodyScroll: document.body.scrollWidth,
      documentClient: document.documentElement.clientWidth,
      documentScroll: document.documentElement.scrollWidth,
    }))

    expect(buttonBoxes).toHaveLength(4)
    for (const button of buttonBoxes) {
      expect(button.width).toBeGreaterThanOrEqual(44)
      expect(button.height).toBeGreaterThanOrEqual(44)
    }
    expect(toolbar.x).toBeGreaterThanOrEqual(viewport.x - 0.5)
    expect(toolbar.x + toolbar.width).toBeLessThanOrEqual(viewport.x + viewport.width + 0.5)
    expect(toolbar.y).toBeGreaterThanOrEqual(viewport.y - 0.5)
    expect(toolbar.y + toolbar.height).toBeLessThanOrEqual(viewport.y + viewport.height + 0.5)
    expect(toolbar.x).toBeGreaterThanOrEqual(canvas.x - 0.5)
    expect(toolbar.x + toolbar.width).toBeLessThanOrEqual(canvas.x + canvas.width + 0.5)
    expect(toolbar.y).toBeGreaterThanOrEqual(canvas.y - 0.5)
    expect(toolbar.y + toolbar.height).toBeLessThanOrEqual(canvas.y + canvas.height + 0.5)
    expect(widths.documentScroll).toBeLessThanOrEqual(widths.documentClient)
    expect(widths.bodyScroll).toBeLessThanOrEqual(widths.bodyClient)
    expect(pageErrors).toEqual([])
    expect(consoleErrors).toEqual([])
  } finally {
    await context.close()
  }
})

test('Chroma preview zoom preserves video zoom across frame and preview URL refreshes', async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 800 })
  const routes = await installChromaZoomRoutes(page)
  await loadChromaZoomMedia(page, chromaZoomVideoPath)

  const toolbar = page.getByRole('toolbar', { name: '预览缩放控制' })
  const image = page.locator('.chroma-preview-image')
  await expect(toolbar).toBeVisible()
  await toolbar.getByRole('button', { name: '实际大小' }).click()
  await expect(toolbar.getByRole('status')).toHaveText('100%')

  await page.getByRole('slider', { name: '视频时间轴' }).fill('1')
  await expect.poll(() => routes.frameRequests.length).toBe(1)
  await expect(image).toHaveAttribute('src', /zoom-video-frame-1\.svg$/)
  await expect(toolbar.getByRole('status')).toHaveText('100%')

  const imageBox = await requireBox(image, 'video image')
  await page.mouse.click(
    imageBox.x + imageBox.width / 2,
    imageBox.y + imageBox.height / 2,
  )
  await expect.poll(() => routes.videoSampleRequests.length).toBe(1)
  await expect.poll(() => routes.videoPreviewRequests.length).toBe(1)
  await expect(image).toHaveAttribute('src', /zoom-video-keyed-1\.svg$/)
  await expect(toolbar.getByRole('status')).toHaveText('100%')
})

test('Chroma preview zoom stays contained at target widths in light and dark themes', async ({ page }) => {
  const pageErrors: string[] = []
  const consoleErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'error') {
      consoleErrors.push(message.text())
    }
  })
  await installChromaZoomRoutes(page)

  const readToolbarColors = async () => page.getByRole(
    'toolbar',
    { name: '预览缩放控制' },
  ).evaluate((toolbar) => {
    const status = toolbar.querySelector<HTMLElement>('[role="status"]')
    const button = toolbar.querySelector<HTMLElement>('button')
    if (!status || !button) {
      throw new Error('Expected preview toolbar status and button')
    }
    const toolbarStyle = getComputedStyle(toolbar)
    const statusStyle = getComputedStyle(status)
    const buttonStyle = getComputedStyle(button)
    return {
      buttonBackground: buttonStyle.backgroundColor,
      buttonBorder: buttonStyle.borderColor,
      buttonColor: buttonStyle.color,
      buttonVisibility: buttonStyle.visibility,
      statusColor: statusStyle.color,
      statusVisibility: statusStyle.visibility,
      toolbarBackground: toolbarStyle.backgroundColor,
      toolbarBorder: toolbarStyle.borderColor,
      toolbarColor: toolbarStyle.color,
      toolbarVisibility: toolbarStyle.visibility,
    }
  })

  for (const viewportSize of [
    { width: 1440, height: 900 },
    { width: 900, height: 800 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewportSize)
    await loadChromaZoomMedia(page, chromaZoomImagePath)

    const viewport = await requireBox(page.locator('.chroma-preview-viewport'), 'preview viewport')
    const canvas = await requireBox(page.locator('.chroma-preview-canvas'), 'preview canvas')
    const toolbar = await requireBox(
      page.getByRole('toolbar', { name: '预览缩放控制' }),
      'preview toolbar',
    )
    const documentWidths = await page.evaluate(() => ({
      client: document.documentElement.clientWidth,
      scroll: document.documentElement.scrollWidth,
    }))
    const center = {
      x: canvas.x + canvas.width / 2,
      y: canvas.y + canvas.height / 2,
    }

    expect(documentWidths.scroll).toBeLessThanOrEqual(documentWidths.client)
    expect(viewport.x).toBeGreaterThanOrEqual(0)
    expect(viewport.x + viewport.width).toBeLessThanOrEqual(viewportSize.width + 0.5)
    expect(toolbar.x).toBeGreaterThanOrEqual(viewport.x - 0.5)
    expect(toolbar.x + toolbar.width).toBeLessThanOrEqual(viewport.x + viewport.width + 0.5)
    expect(toolbar.y).toBeGreaterThanOrEqual(viewport.y - 0.5)
    expect(toolbar.y + toolbar.height).toBeLessThanOrEqual(viewport.y + viewport.height + 0.5)
    expect(
      center.x >= toolbar.x
      && center.x <= toolbar.x + toolbar.width
      && center.y >= toolbar.y
      && center.y <= toolbar.y + toolbar.height,
    ).toBe(false)
  }

  await page.getByRole('button', { name: '主题设置' }).click()
  await page.getByRole('menuitemradio', { name: '浅色' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  const lightColors = await readToolbarColors()

  await page.getByRole('button', { name: '主题设置' }).click()
  await page.getByRole('menuitemradio', { name: '深色' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  const darkColors = await readToolbarColors()

  for (const colors of [lightColors, darkColors]) {
    for (const color of [
      colors.toolbarBackground,
      colors.toolbarBorder,
      colors.toolbarColor,
      colors.statusColor,
      colors.buttonBackground,
      colors.buttonBorder,
      colors.buttonColor,
    ]) {
      expect(color).not.toBe('')
      expect(color).not.toBe('transparent')
      expect(color).not.toBe('rgba(0, 0, 0, 0)')
    }
    expect(colors.toolbarVisibility).toBe('visible')
    expect(colors.statusVisibility).toBe('visible')
    expect(colors.buttonVisibility).toBe('visible')
  }
  for (const key of [
    'toolbarBackground',
    'toolbarBorder',
    'toolbarColor',
    'statusColor',
    'buttonBackground',
    'buttonBorder',
    'buttonColor',
  ] as const) {
    expect(darkColors[key], `${key} must differ between themes`).not.toBe(lightColors[key])
  }
  expect(pageErrors).toEqual([])
  expect(consoleErrors).toEqual([])
})
