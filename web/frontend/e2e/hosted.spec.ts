import { expect, test } from '@playwright/test'

test.skip(
  process.env.VITE_HOSTED_MODE !== 'true',
  'requires a hosted frontend build',
)

test('hosted mode uploads browser files and hides server output paths', async ({
  page,
}) => {
  await page.route('**/api/uploads', async (route) => {
    const request = route.request()
    expect(request.method()).toBe('POST')
    expect(request.headers()['content-type']).toBe('video/mp4')
    expect(request.headers()['x-upload-filename']).toBe(
      encodeURIComponent('clip.mp4'),
    )
    expect(request.postDataBuffer()).toEqual(Buffer.from('video'))
    await route.fulfill({
      body: JSON.stringify({
        filename: 'clip.mp4',
        reference: 'asset:11111111111111111111111111111111:clip.mp4',
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
