import { readFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { expect, test, type Locator, type Page } from '@playwright/test'

// This suite uses real HTTP and binary media, so run with the dedicated config.
test.skip(
  () => !process.env.DESIGNTOOLKIT_LOCAL_DROP_REAL,
  'Requires a running local backend and playwright.local-drop.config.ts',
)

const worktreeRoot = resolve(process.cwd(), '../..')
const projectRoot = worktreeRoot

async function dropFile(input: Locator, bytes: Buffer, name: string, type: string) {
  await dropFiles(input, [{ bytes, name, type }])
}

async function dropFiles(
  input: Locator,
  files: readonly { bytes: Buffer; name: string; type: string }[],
  uri = '',
) {
  const transfer = await input.page().evaluateHandle(({ data, filename, mime }) => {
    const transfer = new DataTransfer()
    const binary = Uint8Array.from(atob(data), (character) => character.charCodeAt(0))
    const file = new File([binary], filename, { type: mime })
    if ('path' in file) throw new Error('Fixture must not expose a native path')
    transfer.items.add(file)
    return transfer
  }, { data: files[0].bytes.toString('base64'), filename: files[0].name, mime: files[0].type })
  for (const file of files.slice(1)) {
    await input.page().evaluate(({ transfer, data, filename, mime }) => {
      const bytes = Uint8Array.from(atob(data), (character) => character.charCodeAt(0))
      transfer.items.add(new File([bytes], filename, { type: mime }))
    }, { transfer, data: file.bytes.toString('base64'), filename: file.name, mime: file.type })
  }
  if (uri) {
    await input.page().evaluate(({ transfer, uri }) => {
      transfer.setData('text/uri-list', uri)
    }, { transfer, uri })
  }
  await input.dispatchEvent('dragover', { dataTransfer: transfer })
  await input.dispatchEvent('drop', { dataTransfer: transfer })
  await transfer.dispose()
}

async function openChroma(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { exact: true, name: '色键抠图' }).click()
  return page.getByRole('textbox', { name: '输入图片或视频路径' })
}

test('pathless image imports its actual bytes despite a colliding project filename', async ({ page }) => {
  const bytes = await readFile(resolve(worktreeRoot, '.temp/local-drop-fixtures/external-image.png'))
  const input = await openChroma(page)
  const imported = page.waitForResponse((response) =>
    response.url().endsWith('/api/local-paths/import-files') && response.request().method() === 'POST',
  )
  const inspected = page.waitForResponse((response) =>
    response.url().endsWith('/api/chroma-keyer/images/inspect'),
  )
  await dropFile(input, bytes, 'test_frame_1.png', 'image/png')
  const response = await imported
  expect(response.status()).toBe(201)
  const body = await response.json() as { path: string; size: number }
  expect(body.path).toMatch(/[\\/]\.temp[\\/]local-imports[\\/]/)
  expect(body.size).toBe(bytes.length)
  expect(await readFile(body.path)).toEqual(bytes)
  await expect(input).toHaveValue(body.path)
  expect((await inspected).status()).toBe(200)
  await expect(page.getByRole('button', { name: '抠像预览', exact: true })).toBeEnabled()
  expect(await input.inputValue()).not.toBe(resolve(projectRoot, 'assets/frame/test_frame_1.png'))
})

test('pathless MP4 imports and automatically loads the video preview', async ({ page }) => {
  const bytes = await readFile(resolve(worktreeRoot, '.temp/local-drop-fixtures/external-video.mp4'))
  const input = await openChroma(page)
  const imported = page.waitForResponse((response) =>
    response.url().endsWith('/api/local-paths/import-files') && response.request().method() === 'POST',
  )
  const inspected = page.waitForResponse((response) =>
    response.url().endsWith('/api/chroma-keyer/videos/inspect'),
  )
  await dropFile(input, bytes, 'external-greenscreen.mp4', 'video/mp4')
  const response = await imported
  expect(response.status()).toBe(201)
  const body = await response.json() as { path: string }
  expect(await readFile(body.path)).toEqual(bytes)
  await expect(input).toHaveValue(body.path)
  const inspection = await inspected
  expect(inspection.status()).toBe(200)
  expect((await inspection.json()).duration_seconds).toBeGreaterThan(0)
  await expect(page.getByRole('button', { name: '抠像预览', exact: true })).toBeEnabled()
})

test('file URI drop preserves the original path without importing a copy', async ({ page }) => {
  const originalPath = resolve(projectRoot, 'assets/frame/test_frame_1.png')
  const input = await openChroma(page)
  let imports = 0
  page.on('request', (request) => {
    if (request.url().endsWith('/api/local-paths/import-files')) imports += 1
  })
  const inspected = page.waitForResponse((response) =>
    response.url().endsWith('/api/chroma-keyer/images/inspect'),
  )
  const transfer = await page.evaluateHandle((path) => {
    const transfer = new DataTransfer()
    transfer.setData('text/uri-list', `file:///${path.replaceAll('\\', '/')}`)
    return transfer
  }, originalPath)
  await input.dispatchEvent('drop', { dataTransfer: transfer })
  await transfer.dispose()
  expect((await inspected).status()).toBe(200)
  await expect(input).toHaveValue(originalPath)
  expect(imports).toBe(0)
})

test('mixed file and URI drop merges every selected file with existing media inputs', async ({ page }) => {
  const bytes = await readFile(resolve(worktreeRoot, '.temp/local-drop-fixtures/external-image.png'))
  const existing = resolve(projectRoot, 'assets/frame/test_frame_1.png')
  const native = resolve(worktreeRoot, '.temp/local-drop-fixtures/external-video.mp4')
  await page.goto('/')
  await page.getByRole('button', { name: '媒体尺寸修改', exact: true }).click()
  const input = page.getByRole('textbox', { name: '输入媒体路径' })
  await input.fill(existing)
  const imported = page.waitForResponse((response) =>
    response.url().endsWith('/api/local-paths/import-files') && response.request().method() === 'POST',
  )
  await dropFiles(input, [
    { bytes, name: 'first.png', type: 'image/png' },
    { bytes: await readFile(native), name: 'external-video.mp4', type: 'video/mp4' },
  ], `file:///${native.replaceAll('\\', '/')}`)
  const response = await imported
  expect(response.status()).toBe(201)
  const body = await response.json() as { path: string }
  expect(await readFile(body.path)).toEqual(bytes)
  await expect(input).toHaveValue([existing, body.path, native].join('\n'))
})

test('parameter edits survive while a real local import response is pending', async ({ page }) => {
  const bytes = await readFile(resolve(worktreeRoot, '.temp/local-drop-fixtures/external-image.png'))
  let release!: () => void
  const pending = new Promise<void>((resolve) => { release = resolve })
  let backendFinished!: () => void
  const ready = new Promise<void>((resolve) => { backendFinished = resolve })
  await page.route('**/api/local-paths/import-files', async (route) => {
    const response = await route.fetch()
    backendFinished()
    await pending
    await route.fulfill({ response })
  })
  const input = await openChroma(page)
  await dropFile(input, bytes, 'parameter-edit.png', 'image/png')
  await ready
  const gain = page.getByRole('slider', { name: 'Screen Gain', exact: true })
  await gain.fill('1.7')
  await expect(gain).toHaveValue('1.7')
  const inspected = page.waitForResponse((response) =>
    response.url().endsWith('/api/chroma-keyer/images/inspect'),
  )
  release()
  expect((await inspected).status()).toBe(200)
  await expect(input).toHaveValue(/local-imports/)
  await expect(gain).toHaveValue('1.7')
})
