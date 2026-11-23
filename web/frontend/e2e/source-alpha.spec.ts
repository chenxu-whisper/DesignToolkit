import { expect, test } from '@playwright/test'

for (const width of [1440, 390]) {
  test(`exports native source alpha without sampling at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const previews: Record<string, unknown>[] = []
    const exports: Record<string, unknown>[] = []
    let sampleRequests = 0
    let frameRequests = 0
    await page.route('**/api/chroma-keyer/videos/inspect', (route) => route.fulfill({
      json: {
        input_path: 'E:\\media\\native.mov',
        width: 32, height: 16, preview_width: 32, preview_height: 16,
        duration_seconds: 2, fps: 4, estimated_frame_count: 8, timestamp_seconds: 0,
        preview_url: '/api/previews/native.png',
      },
    }))
    await page.route('**/api/previews/*.png', (route) => route.fulfill({
      contentType: 'image/png',
      body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64'),
    }))
    await page.route('**/api/chroma-keyer/videos/preview', (route) => {
      const payload = route.request().postDataJSON()
      previews.push(payload)
      return route.fulfill({
        json: { preview_url: `/api/previews/${payload.view_mode}.png`, timestamp_seconds: payload.timestamp_seconds },
      })
    })
    await page.route('**/api/chroma-keyer/videos/sample', (route) => {
      sampleRequests += 1
      return route.abort()
    })
    await page.route('**/api/chroma-keyer/videos/frame', (route) => {
      frameRequests += 1
      return route.abort()
    })
    await page.route('**/api/tasks/chroma-keyer-video', (route) => {
      exports.push(route.request().postDataJSON())
      return route.fulfill({ json: { task_id: 'source-alpha-task', status: 'queued' } })
    })
    await page.goto('/')
    await page.getByRole('button', { name: '色键抠图', exact: true }).click()
    if (width === 390) {
      await page.getByRole('button', { name: '打开参数检查器' }).click()
    }
    const input = page.getByRole('textbox', { name: '输入图片或视频路径' })
    await input.fill('E:\\media\\native.mov')
    await input.press('Enter')
    const source = page.getByRole('combobox', { name: '透明来源' })
    await expect(source).toHaveValue('chroma_key')
    await source.selectOption('source')
    await expect.poll(() => previews.length).toBe(1)
    expect(previews[0]).toEqual({
      input_path: 'E:\\media\\native.mov',
      timestamp_seconds: 0,
      alpha_source: 'source',
      source_alpha_mode: 'straight',
      samples: [],
      view_mode: 'final',
    })
    const interpretation = page.getByRole('combobox', { name: 'Alpha 解释' })
    await interpretation.selectOption('premultiplied')
    await expect.poll(() => previews.length).toBe(2)
    await page.getByRole('radio', { name: 'Alpha', exact: true }).check()
    await expect.poll(() => previews.length).toBe(3)
    expect(previews[2]).toMatchObject({ view_mode: 'alpha', source_alpha_mode: 'premultiplied' })
    await expect(page.getByRole('combobox', { name: '输出格式' })).toBeDisabled()
    await expect(page.getByRole('slider', { name: 'Screen Gain' })).toHaveCount(0)
    for (const control of [source, interpretation]) {
      await control.scrollIntoViewIfNeeded()
      await expect(control).toBeVisible()
      const box = await control.boundingBox()
      expect(box).not.toBeNull()
      expect(box!.width).toBeGreaterThan(150)
      expect(box!.x).toBeGreaterThanOrEqual(0)
      expect(box!.x + box!.width).toBeLessThanOrEqual(width)
    }
    await page.getByRole('textbox', { name: 'MP4 输出路径（可选）' }).fill('E:\\exports\\native.mp4')
    await page.getByRole('button', { name: '导出 MP4' }).click()
    await expect.poll(() => exports.length).toBe(1)
    expect(exports[0]).toEqual({
      input_path: 'E:\\media\\native.mov',
      output_format: 'packed_alpha_mp4',
      output_path: 'E:\\exports\\native.mp4',
      alpha_source: 'source',
      source_alpha_mode: 'premultiplied',
      samples: [],
    })
    expect(previews).toHaveLength(3)
    expect(sampleRequests).toBe(0)
    expect(frameRequests).toBe(0)
  })
}
