import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { PathInput } from './PathInput'

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

function ControlledPathInput() {
  const [value, setValue] = useState('C:\\input\\clip.mp4')

  return <PathInput label="输入视频路径" onChange={setValue} value={value} />
}

function ControlledBatchPathInput() {
  const [value, setValue] = useState('')

  return <PathInput label="输入视频路径" multiline onChange={setValue} value={value} />
}

function ControlledBatchPathInputWithResolver({
  resolveDroppedFilePaths,
}: {
  resolveDroppedFilePaths: (names: readonly string[]) => Promise<readonly string[]>
}) {
  const [value, setValue] = useState('')

  return (
    <PathInput
      label="输入视频路径"
      multiline
      onChange={setValue}
      resolveDroppedFilePaths={resolveDroppedFilePaths}
      value={value}
    />
  )
}

function ControlledPathInputWithPicker({
  pickPaths,
}: {
  pickPaths: () => Promise<readonly string[]>
}) {
  const [value, setValue] = useState('')

  return <PathInput label="输入媒体路径" onChange={setValue} pickPaths={pickPaths} value={value} />
}

function ControlledBatchPathInputWithPicker({
  initialValue = '',
  pickPaths,
}: {
  initialValue?: string
  pickPaths: () => Promise<readonly string[]>
}) {
  const [value, setValue] = useState(initialValue)

  return (
    <PathInput
      label="输入视频路径"
      multiline
      onChange={setValue}
      pickPaths={pickPaths}
      value={value}
    />
  )
}

function ControlledCommittedPathInput({
  initialValue = '',
  multiline = false,
  onChange,
  onCommit,
  pickPaths,
  resolveDroppedFilePaths,
}: {
  initialValue?: string
  multiline?: boolean
  onChange: (value: string) => void
  onCommit: (value: string) => void
  pickPaths?: () => Promise<readonly string[]>
  resolveDroppedFilePaths?: (names: readonly string[]) => Promise<readonly string[]>
}) {
  const [value, setValue] = useState(initialValue)

  return (
    <PathInput
      label="输入媒体路径"
      multiline={multiline}
      onChange={(nextValue) => {
        onChange(nextValue)
        setValue(nextValue)
      }}
      onCommit={onCommit}
      pickPaths={pickPaths}
      resolveDroppedFilePaths={resolveDroppedFilePaths}
      value={value}
    />
  )
}

describe('PathInput', () => {
  it('commits once on Enter without submitting an enclosing form', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const onCommit = vi.fn()
    const onSubmit = vi.fn()
    render(
      <form
        onSubmit={(event) => {
          event.preventDefault()
          onSubmit()
        }}
      >
        <ControlledCommittedPathInput onChange={onChange} onCommit={onCommit} />
      </form>,
    )
    const input = screen.getByLabelText('输入媒体路径')

    await user.type(input, 'D:\\art\\screen.png')

    expect(onChange).toHaveBeenLastCalledWith('D:\\art\\screen.png')
    expect(onCommit).not.toHaveBeenCalled()

    await user.keyboard('{Enter}')

    expect(onCommit).toHaveBeenCalledOnce()
    expect(onCommit).toHaveBeenLastCalledWith('D:\\art\\screen.png')
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('skips only the blur immediately following Enter', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const onCommit = vi.fn()
    render(<ControlledCommittedPathInput onChange={onChange} onCommit={onCommit} />)
    const input = screen.getByLabelText('输入媒体路径')

    await user.type(input, 'D:\\art\\screen.png')
    await user.keyboard('{Enter}')
    fireEvent.blur(input)

    expect(onCommit).toHaveBeenCalledOnce()

    fireEvent.change(input, { target: { value: 'D:\\art\\replacement.png' } })
    fireEvent.blur(input)

    expect(onCommit).toHaveBeenCalledTimes(2)
    expect(onCommit).toHaveBeenLastCalledWith('D:\\art\\replacement.png')
  })

  it('commits the current single-line path on blur', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const onCommit = vi.fn()
    render(<ControlledCommittedPathInput onChange={onChange} onCommit={onCommit} />)
    const input = screen.getByLabelText('输入媒体路径')

    await user.type(input, 'D:\\art\\screen.png')
    fireEvent.blur(input)

    expect(onCommit).toHaveBeenCalledOnce()
    expect(onCommit).toHaveBeenLastCalledWith('D:\\art\\screen.png')
  })

  it('ignores Enter while an IME composition is active', () => {
    const onChange = vi.fn()
    const onCommit = vi.fn()
    render(
      <ControlledCommittedPathInput
        initialValue="D:\\art\\screen.png"
        onChange={onChange}
        onCommit={onCommit}
      />,
    )

    fireEvent.keyDown(screen.getByLabelText('输入媒体路径'), {
      isComposing: true,
      key: 'Enter',
    })

    expect(onCommit).not.toHaveBeenCalled()
  })

  it('updates and commits the exact path returned by the native picker', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const onCommit = vi.fn()
    const pickPaths = vi.fn().mockResolvedValue(['E:\\media\\screen.png'])
    render(
      <ControlledCommittedPathInput
        multiline
        onChange={onChange}
        onCommit={onCommit}
        pickPaths={pickPaths}
      />,
    )

    await user.click(screen.getByRole('button', { name: '指定输入媒体路径' }))

    await waitFor(() => expect(onCommit).toHaveBeenCalledWith('E:\\media\\screen.png'))
    expect(onChange).toHaveBeenCalledWith('E:\\media\\screen.png')
    expect(onCommit).toHaveBeenCalledOnce()
  })

  it('does not blur-commit the typed path when a picker click is cancelled', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const onCommit = vi.fn()
    const pickPaths = vi.fn().mockResolvedValue([])
    render(
      <ControlledCommittedPathInput
        onChange={onChange}
        onCommit={onCommit}
        pickPaths={pickPaths}
      />,
    )
    const input = screen.getByLabelText('输入媒体路径')

    await user.type(input, 'D:\\typed\\screen.png')
    await user.click(screen.getByRole('button', { name: '指定输入媒体路径' }))

    await waitFor(() => expect(pickPaths).toHaveBeenCalledOnce())
    expect(onCommit).not.toHaveBeenCalled()
  })

  it('does not commit when the native picker is cancelled', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const onCommit = vi.fn()
    const pickPaths = vi.fn().mockResolvedValue([])
    render(
      <ControlledCommittedPathInput
        onChange={onChange}
        onCommit={onCommit}
        pickPaths={pickPaths}
      />,
    )

    await user.click(screen.getByRole('button', { name: '指定输入媒体路径' }))

    await waitFor(() => expect(pickPaths).toHaveBeenCalledOnce())
    expect(onChange).not.toHaveBeenCalled()
    expect(onCommit).not.toHaveBeenCalled()
  })

  it('does not commit when the native picker fails', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const onCommit = vi.fn()
    const pickPaths = vi.fn().mockRejectedValue(new Error('Native picker unavailable'))
    render(
      <ControlledCommittedPathInput
        onChange={onChange}
        onCommit={onCommit}
        pickPaths={pickPaths}
      />,
    )

    await user.click(screen.getByRole('button', { name: '指定输入媒体路径' }))

    await waitFor(() => expect(screen.getByLabelText('本地路径')).toHaveFocus())
    expect(onChange).not.toHaveBeenCalled()
    expect(onCommit).not.toHaveBeenCalled()
  })

  it('merges picker paths with the latest multiline value after awaiting', async () => {
    const user = userEvent.setup()
    const selection = deferred<readonly string[]>()
    const onChange = vi.fn()
    const onCommit = vi.fn()
    const pickPaths = vi.fn(() => selection.promise)
    render(
      <ControlledCommittedPathInput
        initialValue={'C:\\media\\one.mp4'}
        multiline
        onChange={onChange}
        onCommit={onCommit}
        pickPaths={pickPaths}
      />,
    )
    const input = screen.getByLabelText('输入媒体路径')

    await user.click(screen.getByRole('button', { name: '指定输入媒体路径' }))
    fireEvent.change(input, {
      target: { value: 'C:\\media\\one.mp4\nD:\\media\\two.mp4' },
    })
    selection.resolve(['E:\\media\\three.mp4'])

    await waitFor(() =>
      expect(onCommit).toHaveBeenCalledWith(
        'C:\\media\\one.mp4\nD:\\media\\two.mp4\nE:\\media\\three.mp4',
      ),
    )
    expect(input).toHaveValue(
      'C:\\media\\one.mp4\nD:\\media\\two.mp4\nE:\\media\\three.mp4',
    )
  })

  it('updates and commits the exact directly dropped path', () => {
    const onChange = vi.fn()
    const onCommit = vi.fn()
    render(
      <ControlledCommittedPathInput multiline onChange={onChange} onCommit={onCommit} />,
    )

    fireEvent.drop(screen.getByLabelText('输入媒体路径'), {
      dataTransfer: {
        files: [],
        getData: (type: string) => (type === 'text/plain' ? 'D:\\drop\\screen.png' : ''),
      },
    })

    expect(onChange).toHaveBeenCalledWith('D:\\drop\\screen.png')
    expect(onCommit).toHaveBeenCalledOnce()
    expect(onCommit).toHaveBeenCalledWith('D:\\drop\\screen.png')
  })

  it('updates and commits the exact resolved dropped path', async () => {
    const onChange = vi.fn()
    const onCommit = vi.fn()
    const resolveDroppedFilePaths = vi.fn().mockResolvedValue(['E:\\resolved\\screen.png'])
    render(
      <ControlledCommittedPathInput
        multiline
        onChange={onChange}
        onCommit={onCommit}
        resolveDroppedFilePaths={resolveDroppedFilePaths}
      />,
    )

    fireEvent.drop(screen.getByLabelText('输入媒体路径'), {
      dataTransfer: {
        files: [new File(['image'], 'screen.png', { type: 'image/png' })],
        getData: () => '',
      },
    })

    await waitFor(() => expect(onCommit).toHaveBeenCalledWith('E:\\resolved\\screen.png'))
    expect(onChange).toHaveBeenCalledWith('E:\\resolved\\screen.png')
    expect(onCommit).toHaveBeenCalledOnce()
  })

  it('merges resolved drop paths with the latest multiline value after awaiting', async () => {
    const resolution = deferred<readonly string[]>()
    const onChange = vi.fn()
    const onCommit = vi.fn()
    const resolveDroppedFilePaths = vi.fn(() => resolution.promise)
    render(
      <ControlledCommittedPathInput
        initialValue={'C:\\media\\one.mp4'}
        multiline
        onChange={onChange}
        onCommit={onCommit}
        resolveDroppedFilePaths={resolveDroppedFilePaths}
      />,
    )
    const input = screen.getByLabelText('输入媒体路径')

    fireEvent.drop(input, {
      dataTransfer: {
        files: [new File(['video'], 'three.mp4', { type: 'video/mp4' })],
        getData: () => '',
      },
    })
    await waitFor(() => expect(resolveDroppedFilePaths).toHaveBeenCalledOnce())
    fireEvent.change(input, {
      target: { value: 'C:\\media\\one.mp4\nD:\\media\\two.mp4' },
    })
    resolution.resolve(['E:\\media\\three.mp4'])

    await waitFor(() =>
      expect(onCommit).toHaveBeenCalledWith(
        'C:\\media\\one.mp4\nD:\\media\\two.mp4\nE:\\media\\three.mp4',
      ),
    )
    expect(input).toHaveValue(
      'C:\\media\\one.mp4\nD:\\media\\two.mp4\nE:\\media\\three.mp4',
    )
  })

  it('updates and commits the exact path applied from the editor', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const onCommit = vi.fn()
    render(
      <ControlledCommittedPathInput
        initialValue="C:\\old\\screen.png"
        multiline
        onChange={onChange}
        onCommit={onCommit}
      />,
    )

    await user.click(screen.getByRole('button', { name: '指定输入媒体路径' }))
    const editor = screen.getByLabelText('本地路径')
    await user.clear(editor)
    await user.type(editor, 'D:\\edited\\screen.png')
    await user.click(screen.getByRole('button', { name: '应用路径' }))

    expect(onChange).toHaveBeenCalledWith('D:\\edited\\screen.png')
    expect(onCommit).toHaveBeenCalledOnce()
    expect(onCommit).toHaveBeenCalledWith('D:\\edited\\screen.png')
  })

  it('does not commit when the path editor is cancelled', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const onCommit = vi.fn()
    render(
      <ControlledCommittedPathInput
        initialValue="C:\\old\\screen.png"
        onChange={onChange}
        onCommit={onCommit}
      />,
    )

    await user.click(screen.getByRole('button', { name: '指定输入媒体路径' }))
    const editor = screen.getByLabelText('本地路径')
    await user.clear(editor)
    await user.type(editor, 'D:\\edited\\screen.png')
    await user.click(screen.getByRole('button', { name: '取消' }))

    expect(onChange).not.toHaveBeenCalled()
    expect(onCommit).not.toHaveBeenCalled()
  })

  it('does not commit multiline manual changes on Enter or blur', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const onCommit = vi.fn()
    render(
      <ControlledCommittedPathInput multiline onChange={onChange} onCommit={onCommit} />,
    )
    const input = screen.getByLabelText('输入媒体路径')

    await user.type(input, 'C:\\art\\one.png{Enter}D:\\art\\two.png')
    fireEvent.blur(input)

    expect(onChange).toHaveBeenLastCalledWith('C:\\art\\one.png\nD:\\art\\two.png')
    expect(onCommit).not.toHaveBeenCalled()
  })

  it('lets users specify a path through the folder icon editor', async () => {
    const user = userEvent.setup()

    render(<ControlledPathInput />)

    await user.click(screen.getByRole('button', { name: '指定输入视频路径' }))
    await waitFor(() => expect(screen.getByLabelText('本地路径')).toHaveFocus())
    await user.clear(screen.getByLabelText('本地路径'))
    await user.type(screen.getByLabelText('本地路径'), 'D:\\batch\\clip-a.mp4')
    await user.click(screen.getByRole('button', { name: '应用路径' }))

    expect(screen.getByLabelText('输入视频路径')).toHaveValue('D:\\batch\\clip-a.mp4')
    expect(screen.queryByRole('dialog', { name: '指定输入视频路径' })).not.toBeInTheDocument()
  })

  it('keeps the original path when the folder icon editor is cancelled', async () => {
    const user = userEvent.setup()

    render(<ControlledPathInput />)

    await user.click(screen.getByRole('button', { name: '指定输入视频路径' }))
    await user.clear(screen.getByLabelText('本地路径'))
    await user.type(screen.getByLabelText('本地路径'), 'D:\\batch\\clip-a.mp4')
    await user.click(screen.getByRole('button', { name: '取消' }))

    expect(screen.getByLabelText('输入视频路径')).toHaveValue('C:\\input\\clip.mp4')
    expect(screen.queryByRole('dialog', { name: '指定输入视频路径' })).not.toBeInTheDocument()
  })

  it('fills a path from dropped plain text', () => {
    render(<ControlledPathInput />)

    fireEvent.drop(screen.getByLabelText('输入视频路径'), {
      dataTransfer: {
        files: [],
        getData: (type: string) => (type === 'text/plain' ? 'D:\\batch\\clip-a.mp4' : ''),
      },
    })

    expect(screen.getByLabelText('输入视频路径')).toHaveValue('D:\\batch\\clip-a.mp4')
  })

  it('fills multiple paths from dropped file URIs', () => {
    render(<ControlledBatchPathInput />)

    fireEvent.drop(screen.getByLabelText('输入视频路径'), {
      dataTransfer: {
        files: [],
        getData: (type: string) =>
          type === 'text/uri-list'
            ? 'file:///C:/batch/clip-a.mp4\r\nfile:///D:/batch/clip-b.webm'
            : '',
      },
    })

    expect(screen.getByLabelText('输入视频路径')).toHaveValue(
      'C:\\batch\\clip-a.mp4\nD:\\batch\\clip-b.webm',
    )
  })

  it('shows a useful error when dropped files do not expose real paths', () => {
    render(<ControlledBatchPathInput />)

    fireEvent.drop(screen.getByLabelText('输入视频路径'), {
      dataTransfer: {
        files: [new File(['video'], 'clip-a.mp4', { type: 'video/mp4' })],
        getData: () => '',
      },
    })

    expect(screen.getByText('浏览器未提供文件真实路径，请使用文件夹按钮粘贴本地路径')).toBeInTheDocument()
  })

  it('resolves dropped file names when the browser does not expose real paths', async () => {
    const resolveDroppedFilePaths = vi
      .fn()
      .mockResolvedValue(['E:\\project\\assets\\video\\clip-a.mp4', 'E:\\project\\assets\\video\\clip-b.webm'])
    render(<ControlledBatchPathInputWithResolver resolveDroppedFilePaths={resolveDroppedFilePaths} />)

    fireEvent.drop(screen.getByLabelText('输入视频路径'), {
      dataTransfer: {
        files: [
          new File(['video-a'], 'clip-a.mp4', { type: 'video/mp4' }),
          new File(['video-b'], 'clip-b.webm', { type: 'video/webm' }),
        ],
        getData: () => '',
      },
    })

    await waitFor(() =>
      expect(screen.getByLabelText('输入视频路径')).toHaveValue(
        'E:\\project\\assets\\video\\clip-a.mp4\nE:\\project\\assets\\video\\clip-b.webm',
      ),
    )
    expect(resolveDroppedFilePaths).toHaveBeenCalledWith(['clip-a.mp4', 'clip-b.webm'])
  })

  it('appends file-only drops to existing multiline paths', async () => {
    const resolveDroppedFilePaths = vi
      .fn()
      .mockResolvedValueOnce(['E:\\project\\assets\\video\\clip-a.mp4'])
      .mockResolvedValueOnce(['E:\\project\\assets\\video\\clip-b.webm'])
    render(<ControlledBatchPathInputWithResolver resolveDroppedFilePaths={resolveDroppedFilePaths} />)
    const input = screen.getByLabelText('输入视频路径')

    fireEvent.drop(input, {
      dataTransfer: {
        files: [new File(['video-a'], 'clip-a.mp4', { type: 'video/mp4' })],
        getData: () => '',
      },
    })
    await waitFor(() => expect(input).toHaveValue('E:\\project\\assets\\video\\clip-a.mp4'))

    fireEvent.drop(input, {
      dataTransfer: {
        files: [new File(['video-b'], 'clip-b.webm', { type: 'video/webm' })],
        getData: () => '',
      },
    })

    await waitFor(() =>
      expect(input).toHaveValue(
        'E:\\project\\assets\\video\\clip-a.mp4\nE:\\project\\assets\\video\\clip-b.webm',
      ),
    )
  })

  it('uses the native picker from the folder icon when available', async () => {
    const user = userEvent.setup()
    const pickPaths = vi.fn().mockResolvedValue(['E:\\project\\assets\\video\\clip-a.mp4'])
    render(<ControlledPathInputWithPicker pickPaths={pickPaths} />)

    await user.click(screen.getByRole('button', { name: '指定输入媒体路径' }))

    await waitFor(() =>
      expect(screen.getByLabelText('输入媒体路径')).toHaveValue('E:\\project\\assets\\video\\clip-a.mp4'),
    )
    expect(screen.queryByRole('dialog', { name: '指定输入媒体路径' })).not.toBeInTheDocument()
  })

  it('appends native picker paths to multiline values', async () => {
    const user = userEvent.setup()
    const pickPaths = vi
      .fn()
      .mockResolvedValue(['E:\\project\\assets\\video\\clip-b.webm'])
    render(
      <ControlledBatchPathInputWithPicker
        initialValue={'E:\\project\\assets\\video\\clip-a.mp4'}
        pickPaths={pickPaths}
      />,
    )

    await user.click(screen.getByRole('button', { name: '指定输入视频路径' }))

    await waitFor(() =>
      expect(screen.getByLabelText('输入视频路径')).toHaveValue(
        'E:\\project\\assets\\video\\clip-a.mp4\nE:\\project\\assets\\video\\clip-b.webm',
      ),
    )
  })

  it('falls back to manual path editing when the native picker fails', async () => {
    const user = userEvent.setup()
    const pickPaths = vi.fn().mockRejectedValue(new Error('Native picker unavailable'))
    render(<ControlledPathInputWithPicker pickPaths={pickPaths} />)

    await user.click(screen.getByRole('button', { name: '指定输入媒体路径' }))

    await waitFor(() => expect(screen.getByLabelText('本地路径')).toHaveFocus())
    expect(screen.getByText('无法打开系统选择器，请手动输入本地路径')).toBeInTheDocument()
  })
})
