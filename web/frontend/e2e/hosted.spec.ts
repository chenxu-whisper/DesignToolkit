import { expect, test } from '@playwright/test'

test('hosted mode uploads browser files and hides server output paths', async ({
  page,
}) => {
  await page.route('**/api/uploads', async (route) => {
    const request = route.request()
    expect(request.method()).toBe('POST')
    expect(request.headers()['content-type']).toBe('video/mp4')
    const uploadId = request.headers()['x-upload-id']
    expect(uploadId).toMatch(/^[a-f0-9]{32}$/)
    expect(request.headers()['x-upload-filename']).toBe(
      encodeURIComponent('clip.mp4'),
    )
    expect(request.postDataBuffer()).toEqual(Buffer.from('video'))
    await route.fulfill({
      body: JSON.stringify({
        filename: 'clip.mp4',
        reference: `asset:${uploadId}:clip.mp4`,
        size: 5,
      }),
      contentType: 'application/json',
      status: 201,
    })
  })
  await page.goto('/')

  await page.locator('input[type="file"]').first().setInputFiles({
    buffer: Buffer.from('video'),
    mimeType: 'video/mp4',
    name: 'clip.mp4',
  })

  await expect(page.getByRole('textbox', { name: '输入视频' })).toHaveValue(
    'clip.mp4',
  )
  await expect(page.getByLabel('输出目录')).toHaveCount(0)
})

test('hosted mode rolls back an upload whose response is lost', async ({
  page,
}) => {
  let postedUploadId = ''
  let resolveDeletedUpload: (uploadId: string) => void
  const deletedUpload = new Promise<string>((resolve) => {
    resolveDeletedUpload = resolve
  })
  await page.route('**/api/uploads/*', async (route) => {
    const request = route.request()
    expect(request.method()).toBe('DELETE')
    const uploadId = new URL(request.url()).pathname.split('/').at(-1) ?? ''
    resolveDeletedUpload(uploadId)
    await route.fulfill({ status: 404 })
  })
  await page.route('**/api/uploads', async (route) => {
    const request = route.request()
    postedUploadId = request.headers()['x-upload-id'] ?? ''
    expect(postedUploadId).toMatch(/^[a-f0-9]{32}$/)
    await route.abort('connectionfailed')
  })
  await page.goto('/')

  await page.locator('input[type="file"]').first().setInputFiles({
    buffer: Buffer.from('video'),
    mimeType: 'video/mp4',
    name: 'clip.mp4',
  })

  await expect(deletedUpload).resolves.toBe(postedUploadId)
})
