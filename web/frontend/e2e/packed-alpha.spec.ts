import { test, expect, type Page } from '@playwright/test'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
// @ts-expect-error Standalone Node fixture helper is intentionally independent of the app build.
import { fixture, productionFixture, servePlayer, artifacts, playerDir } from './scripts/packed-alpha-fixture.mjs'

let files: Awaited<ReturnType<typeof fixture>>
let server: Awaited<ReturnType<typeof servePlayer>>
test.beforeAll(async ({ browser }) => {
  console.log('Browser:', browser.browserType().name(), browser.version())
  files = await fixture()
  server = await servePlayer()
})
test.afterAll(async () => { await server?.close() })

async function open(page: Page) {
  await page.goto(server.url)
  await expect(page.getByRole('heading', { name: 'Packed Alpha Player' })).toBeVisible()
}
async function load(page: Page, input = files) {
  await open(page)
  await page.getByLabel('MP4 video').setInputFiles(input.mp4)
  await page.getByLabel('JSON metadata').setInputFiles(input.json)
  await expect(page.getByRole('status')).toContainText('Ready')
}
async function pixels(page: Page) {
  return page.evaluate(() => {
    const player = (window as any).player
    player.render()
    const canvas = player.canvas as HTMLCanvasElement
    const gl = canvas.getContext('webgl')!
    const read = (x: number, y: number) => {
      const value = new Uint8Array(4)
      gl.readPixels(x, canvas.height - 1 - y, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, value)
      return Array.from(value)
    }
    const composite = (background: number, x: number, y: number) => {
      const output = document.createElement('canvas')
      output.width = canvas.width
      output.height = canvas.height
      const context = output.getContext('2d')!
      context.fillStyle = `rgb(${background},${background},${background})`
      context.fillRect(0, 0, output.width, output.height)
      context.drawImage(canvas, 0, 0)
      return Array.from(context.getImageData(x, y, 1, 1).data)
    }
    return {
      size: [canvas.width, canvas.height], attributes: gl.getContextAttributes(),
      transparent: read(8, 8), half: read(32, 8), opaque: read(54, 8),
      seam: read(64, 8), bottom: read(54, 32),
      dark: [8, 32, 54].map(x => composite(24, x, 8)),
      light: [8, 32, 54].map(x => composite(240, x, 8)),
    }
  })
}
function near(actual: number[], expected: number[], tolerance = 8) {
  actual.forEach((value, index) => expect(Math.abs(value - expected[index])).toBeLessThanOrEqual(tolerance))
}

test('local H.264 restores straight alpha, odd crop, orientation and seam pixel centers', async ({ page }, info) => {
  await load(page)
  const evidence = await pixels(page)
  console.log('DECODED PIXELS', JSON.stringify(evidence))
  await info.attach('decoded-pixels.json', { body: JSON.stringify(evidence, null, 2), contentType: 'application/json' })
  expect(evidence.size).toEqual([65, 33])
  expect(evidence.attributes).toMatchObject({ alpha: true, premultipliedAlpha: false })
  near(evidence.transparent, [192, 192, 192, 0])
  near(evidence.half, [192, 192, 192, 128])
  near(evidence.opaque, [192, 192, 192, 255])
  near(evidence.seam, [192, 192, 192, 255], 12)
  near(evidence.bottom, [64, 64, 64, 255], 12)
  near(evidence.dark[0], [24, 24, 24, 255])
  near(evidence.dark[1], [108, 108, 108, 255])
  near(evidence.dark[2], [192, 192, 192, 255])
  near(evidence.light[0], [240, 240, 240, 255])
  near(evidence.light[1], [216, 216, 216, 255])
  near(evidence.light[2], [192, 192, 192, 255])
  for (const background of ['dark', 'light']) {
    await page.getByLabel('Background').selectOption(background)
    await page.screenshot({ path: join(artifacts, `preview-${background}.png`), fullPage: true })
    await info.attach(`preview-${background}`, {
      path: join(artifacts, `preview-${background}.png`), contentType: 'image/png',
    })
  }
})

test('classic script loads MP4 and JSON offline from file:// with no network requests', async ({ page }) => {
  const network: string[] = []
  page.on('request', request => {
    if (/^https?:/.test(request.url())) network.push(request.url())
  })
  await page.goto(pathToFileURL(join(playerDir, 'index.html')).href)
  await expect(page.getByRole('heading', { name: 'Packed Alpha Player' })).toBeVisible()
  await page.getByLabel('MP4 video').setInputFiles(files.mp4)
  await page.getByLabel('JSON metadata').setInputFiles(files.json)
  await expect(page.getByRole('status')).toContainText('Ready')
  near((await pixels(page)).half, [192, 192, 192, 128])
  expect(network).toEqual([])
})

test('actual production encoder and JSON cross-validate with decoded browser alpha', async ({ page }, info) => {
  const produced = await productionFixture()
  await load(page, produced)
  const evidence = await pixels(page)
  console.log('PRODUCTION PIXELS', JSON.stringify(evidence))
  await info.attach('production-pixels.json', { body: JSON.stringify(evidence, null, 2), contentType: 'application/json' })
  expect(evidence.size).toEqual([65, 33])
  near(evidence.transparent, [192, 192, 192, 0])
  near(evidence.half, [192, 192, 192, 128])
  near(evidence.opaque, [192, 192, 192, 255])
  near(evidence.seam, [192, 192, 192, 255], 12)
  near(evidence.bottom, [64, 64, 64, 255], 12)
  near(evidence.dark[1], [108, 108, 108, 255])
  near(evidence.light[1], [216, 216, 216, 255])
})

test('controls play, pause, seek, mute and switch backgrounds on narrow screens', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await load(page)
  await page.getByRole('button', { name: 'Play', exact: true }).click()
  await expect.poll(() => page.evaluate(() => (window as any).player.video.currentTime)).toBeGreaterThan(0)
  await page.getByRole('button', { name: 'Pause', exact: true }).click()
  expect(await page.evaluate(() => (window as any).player.video.paused)).toBe(true)
  await page.getByLabel('Seek').fill('1.4')
  await expect.poll(() => page.evaluate(() => (window as any).player.video.currentTime)).toBeCloseTo(1.4, 1)
  await expect.poll(async () => (await pixels(page)).opaque[0]).toBeLessThan(105)
  await page.getByRole('button', { name: 'Unmute', exact: true }).click()
  expect(await page.evaluate(() => (window as any).player.video.muted)).toBe(false)
  for (const value of ['light', 'dark', 'checker']) {
    await page.getByLabel('Background').selectOption(value)
    await expect(page.locator('#stage')).toHaveAttribute('data-background', value)
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: join(artifacts, 'preview-mobile.png'), fullPage: true })
})

test('rejects malformed metadata and mismatched dimensions, then recovers with valid files', async ({ page }) => {
  await open(page)
  await page.getByLabel('MP4 video').setInputFiles(files.mp4)
  for (const metadata of [
    '{bad json', JSON.stringify({ ...files.metadata, version: 2 }),
    JSON.stringify({ ...files.metadata, layout: 'alpha_left_color_right' }),
    JSON.stringify({ ...files.metadata, alpha_mode: 'premultiplied' }),
    JSON.stringify({ ...files.metadata, original_width: 67 }),
    JSON.stringify({ ...files.metadata, stored_width: 68 }),
  ]) {
    await page.getByLabel('JSON metadata').setInputFiles({
      name: 'invalid.json', mimeType: 'application/json', buffer: Buffer.from(metadata),
    })
    await expect(page.getByRole('status')).toContainText(/metadata|dimensions/i)
    await expect(page.getByRole('status')).toHaveAttribute('data-error', 'true')
    await expect(page.getByRole('button', { name: 'Play', exact: true })).toBeDisabled()
  }
  await page.getByLabel('JSON metadata').setInputFiles(files.json)
  await expect(page.getByRole('status')).toContainText('Ready')
})

test('reports failed decode and unsupported WebGL without enabling playback', async ({ page }) => {
  await open(page)
  await page.getByLabel('MP4 video').setInputFiles(join(artifacts, 'broken.mp4'))
  await page.getByLabel('JSON metadata').setInputFiles(files.json)
  await expect(page.getByRole('status')).toContainText(/decode/i)
  await expect(page.getByRole('button', { name: 'Play', exact: true })).toBeDisabled()
  await page.addInitScript(() => {
    const original = HTMLCanvasElement.prototype.getContext
    HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, type: any, ...args: any[]) {
      if (type === 'webgl') return null
      return original.call(this, type, ...args)
    } as any
  })
  await page.reload()
  await expect(page.getByRole('status')).toContainText(/WebGL.*unavailable/i)
  await expect(page.getByRole('button', { name: 'Play', exact: true })).toBeDisabled()
})

test('context loss stops playback, restores paused frame, and destroy releases local URLs', async ({ page }) => {
  await load(page)
  await page.getByRole('button', { name: 'Play', exact: true }).click()
  await page.evaluate(() => {
    const player = (window as any).player
    ;(window as any).loss = player.canvas.getContext('webgl').getExtension('WEBGL_lose_context')
    ;(window as any).loss.loseContext()
  })
  await expect(page.getByRole('status')).toContainText(/context lost/i)
  expect(await page.evaluate(() => (window as any).player.video.paused)).toBe(true)
  await page.evaluate(() => (window as any).loss.restoreContext())
  await expect(page.getByRole('status')).toContainText(/Paused/i)
  near((await pixels(page)).half, [192, 192, 192, 128])
  const cleanup = await page.evaluate(async () => {
    const player = (window as any).player
    const url = player.video.src
    player.destroy()
    let revoked = false
    try { await fetch(url) } catch { revoked = true }
    return { status: player.status, source: player.video.getAttribute('src'), revoked }
  })
  expect(cleanup).toEqual({ status: 'destroyed', source: null, revoked: true })
})

for (const mismatched of [false, true]) {
  test(`context restoration cannot validate an incomplete ${mismatched ? 'mismatched' : 'matching'} load`, async ({ page }) => {
    await open(page)
    await page.getByLabel('MP4 video').setInputFiles(files.mp4)
    const metadata = mismatched ?
      { ...files.metadata, original_width: 67, stored_width: 68 } : files.metadata
    await page.evaluate(value => {
      const player = (window as any).player
      const video = player.video as HTMLVideoElement
      const original = video.addEventListener
      // Hold only the player's completion hook; the browser still decodes real H.264.
      video.addEventListener = function (type: any, listener: any, options: any) {
        if (type !== 'loadeddata') original.call(this, type, listener, options)
      } as any
      ;(window as any).resumeLoadEvents = () => { video.addEventListener = original }
      const file = (document.getElementById('video-file') as HTMLInputElement).files![0]
      ;(window as any).pendingLoad = player.load(file, value).then(
        () => 'resolved', (error: DOMException) => error.name,
      )
      ;(window as any).loss = player.gl.getExtension('WEBGL_lose_context')
    }, metadata)
    await expect.poll(() => page.evaluate(() => (window as any).player.video.readyState)).toBeGreaterThanOrEqual(2)
    expect(await page.evaluate(() => {
      const player = (window as any).player
      return { status: player.status, size: [player.canvas.width, player.canvas.height] }
    })).toEqual({ status: 'loading', size: [640, 360] })
    await page.evaluate(() => (window as any).loss.loseContext())
    await expect(page.getByRole('status')).toContainText(/context lost/i)
    expect(await page.evaluate(() => (window as any).pendingLoad)).toBe('AbortError')
    await page.evaluate(() => (window as any).loss.restoreContext())
    await expect.poll(() => page.evaluate(() => (window as any).player.status)).not.toBe('context-lost')
    const restored = await page.evaluate(async () => {
      const player = (window as any).player
      let playError = ''
      let seekError = ''
      try { await player.play() } catch (error) { playError = String(error) }
      try { player.seek(1) } catch (error) { seekError = String(error) }
      return { status: player.status, rendered: player.render(),
        size: [player.canvas.width, player.canvas.height], playError, seekError }
    })
    expect(restored.status).toBe('empty')
    expect(restored.rendered).toBe(false)
    expect(restored.size).toEqual([640, 360])
    expect(restored.playError).toMatch(/valid MP4 and metadata/i)
    expect(restored.seekError).toMatch(/valid video/i)
    await expect(page.getByRole('button', { name: 'Play', exact: true })).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Unmute', exact: true })).toBeDisabled()
    await expect(page.getByLabel('Seek')).toBeDisabled()
    await page.evaluate(() => (window as any).resumeLoadEvents())
    if (mismatched) {
      await page.getByLabel('JSON metadata').setInputFiles({
        name: 'mismatched.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(metadata)),
      })
      await expect(page.getByRole('status')).toContainText(/dimensions do not match/i)
      await expect(page.getByRole('button', { name: 'Play', exact: true })).toBeDisabled()
    }
    await page.getByLabel('JSON metadata').setInputFiles(files.json)
    await expect(page.getByRole('status')).toContainText('Ready')
    expect((await pixels(page)).size).toEqual([65, 33])
    near((await pixels(page)).half, [192, 192, 192, 128])
  })
}

test('reusable API reports rejected playback and texture security failures', async ({ page }) => {
  await load(page)
  const result = await page.evaluate(async () => {
    const player = (window as any).player
    const original = player.video.play
    player.video.play = () => Promise.reject(new DOMException('Blocked', 'NotAllowedError'))
    let rejected = ''
    try { await player.play() } catch (error) { rejected = String(error) }
    player.video.play = original
    const gl = player.canvas.getContext('webgl')
    gl.texImage2D = () => { throw new DOMException('CORS', 'SecurityError') }
    player.render()
    return { exported: typeof (window as any).PackedAlphaPlayer, rejected, status: player.status }
  })
  expect(result.exported).toBe('function')
  expect(result.rejected).toMatch(/playback/i)
  expect(result.status).toBe('error')
  await expect(page.getByRole('status')).toContainText(/texture.*CORS/i)
})

test('replacement loads revoke old URLs, pause stops callbacks, and pending load is cancelled by destroy', async ({ page }) => {
  await load(page)
  const result = await page.evaluate(async () => {
    const player = (window as any).player
    const oldURL = player.video.src
    const metadata = player.metadata
    const file = (document.getElementById('video-file') as HTMLInputElement).files![0]
    await player.load(file, metadata)
    let oldRevoked = false
    try { await fetch(oldURL) } catch { oldRevoked = true }
    await player.play()
    player.pause()
    const paused = player.video.paused && player._frame === null
    const pending = player.load(file, metadata)
    player.destroy()
    player.destroy()
    let cancellation = ''
    try { await pending } catch (error: any) { cancellation = error.name }
    return { oldRevoked, paused, cancellation, status: player.status,
      source: player.video.getAttribute('src'), resources: player._resources }
  })
  expect(result).toEqual({ oldRevoked: true, paused: true, cancellation: 'AbortError',
    status: 'destroyed', source: null, resources: null })
})

test('seam clamp holds when canvas output is scaled above source size', async ({ page }) => {
  await load(page)
  const evidence = await page.evaluate(() => {
    const player = (window as any).player
    player.canvas.width = 260
    player.canvas.height = 132
    player.render()
    const gl = player.canvas.getContext('webgl')
    const sample = new Uint8Array(4)
    gl.readPixels(259, 99, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, sample)
    return Array.from(sample)
  })
  near(evidence, [192, 192, 192, 255], 12)
})
