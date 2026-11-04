import { spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { readFile, mkdir, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export const root = resolve(dirname(fileURLToPath(import.meta.url)), '../../../..')
export const artifacts = join(root, '.temp/packed-alpha-player')
export const playerDir = join(root, 'src/designtoolkit/chroma_keyer/player')

function pythonExecutable() {
  return process.env.PACKED_ALPHA_PYTHON || join(root, '.venv/Scripts/python.exe')
}

export async function productionFixture() {
  const generated = spawnSync(pythonExecutable(), [
    join(root, 'web/frontend/e2e/scripts/packed-alpha-production-fixture.py'), artifacts,
  ], { encoding: 'utf8', timeout: 60000,
    env: { ...process.env, PYTHONPATH: join(root, 'src'), PYTHONDONTWRITEBYTECODE: '1' } })
  if (generated.status !== 0) throw new Error(generated.stderr || generated.error?.message)
  const result = JSON.parse(generated.stdout.trim())
  console.log('Production encoder:', JSON.stringify(result))
  return result
}

export async function fixture() {
  await mkdir(artifacts, { recursive: true })
  const python = pythonExecutable()
  const discovery = spawnSync(python, ['-c',
    'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())'], { encoding: 'utf8' })
  if (discovery.status !== 0) throw new Error(discovery.stderr || discovery.error?.message)
  const metadata = {
    version: 1, layout: 'color_left_alpha_right', alpha_mode: 'straight',
    original_width: 65, original_height: 33, stored_width: 66, stored_height: 34,
    fps: 20, frame_count: 40, duration_seconds: 2,
  }
  const frames = Buffer.alloc(132 * 34 * 3 * 40)
  for (let frame = 0; frame < 40; frame++) {
    for (let y = 0; y < 33; y++) {
      for (let x = 0; x < 65; x++) {
        const color = y < 16 ? (frame < 20 ? 192 : 96) : (frame < 20 ? 64 : 32)
        const alpha = x < 20 ? 0 : x < 44 ? 128 : 255
        const offset = ((frame * 34 + y) * 132 + x) * 3
        frames.fill(color, offset, offset + 3)
        frames.fill(alpha, offset + 66 * 3, offset + 66 * 3 + 3)
      }
    }
  }
  const mp4 = join(artifacts, 'odd-packed.mp4')
  const json = join(artifacts, 'odd-packed.json')
  const encoded = spawnSync(discovery.stdout.trim(), [
    '-hide_banner', '-loglevel', 'error', '-y', '-f', 'rawvideo',
    '-pixel_format', 'rgb24', '-video_size', '132x34', '-framerate', '20',
    '-i', 'pipe:0', '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
    '-crf', '18', '-preset', 'medium', '-movflags', '+faststart', mp4,
  ], { input: frames, maxBuffer: 8 * 1024 * 1024 })
  if (encoded.status !== 0) throw new Error(encoded.stderr?.toString() || encoded.error?.message)
  await writeFile(json, JSON.stringify(metadata))
  await writeFile(join(artifacts, 'broken.mp4'), 'not a video')
  console.log(`Fixture: H.264/yuv420p, 132x34, 40 frames; ${mp4}`)
  return { mp4, json, metadata }
}

export async function servePlayer() {
  // Expose only the four player assets, never arbitrary local paths.
  const server = createServer(async (request, response) => {
    const name = new URL(request.url, 'http://localhost').pathname.slice(1) || 'index.html'
    if (!['index.html', 'player.js', 'player.css', 'README.md'].includes(name)) {
      response.writeHead(404).end()
      return
    }
    try {
      const content = await readFile(join(playerDir, name))
      response.setHeader('Content-Type', name.endsWith('.js') ? 'text/javascript' :
        name.endsWith('.css') ? 'text/css' : 'text/html')
      response.end(content)
    } catch {
      // A missing entry point stays a browser assertion failure during the red phase.
      if (name === 'index.html') response.end('<!doctype html><title>Missing player</title>')
      else response.writeHead(404).end()
    }
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  return {
    url: `http://127.0.0.1:${server.address().port}`,
    close: () => new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve())),
  }
}

// Run only this suite with no inherited React/hosted webServer configuration.
if (process.argv.includes('--test')) {
  await mkdir(artifacts, { recursive: true })
  const config = join(artifacts, 'playwright.config.cjs')
  await writeFile(config, `module.exports = ${JSON.stringify({
    testDir: join(root, 'web/frontend/e2e'),
    testMatch: 'packed-alpha.spec.ts',
    workers: 1,
    timeout: 30000,
    outputDir: join(artifacts, 'results'),
    reporter: [['list']],
    use: { browserName: 'chromium', channel: process.env.PACKED_ALPHA_BROWSER || 'chrome', headless: true,
      launchOptions: { args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] } },
  })}`)
  const result = spawnSync(process.execPath, [
    join(root, 'web/frontend/node_modules/@playwright/test/cli.js'),
    'test', '--config', config, ...process.argv.slice(process.argv.indexOf('--test') + 1),
  ], { stdio: 'inherit' })
  process.exitCode = result.status ?? 1
}
