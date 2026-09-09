import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  deleteUploadedAsset as deleteUploadedAssetWithQueue,
  resetUploadCleanupQueueForTests,
  uploadFiles as uploadFilesWithQueue,
} from '../services/apiClient'
import { PathInput } from './PathInput'

afterEach(() => {
  resetUploadCleanupQueueForTests()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function deferred<T>() {
  let reject!: (reason?: unknown) => void
  let resolve!: (value: T | PromiseLike<T>) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, reject, resolve }
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

function ControlledUploadPathInput({
  deleteUploadedAsset,
  disabled = false,
  initialValue = '',
  onUploadPendingChange,
  uploadFiles,
}: {
  deleteUploadedAsset?: (reference: string) => Promise<void>
  disabled?: boolean
  initialValue?: string
  onUploadPendingChange?: (pending: boolean) => void
  uploadFiles: (
    files: readonly File[],
    onProgress: (percentage: number) => void,
    signal?: AbortSignal,
  ) => Promise<readonly string[]>
}) {
  const [value, setValue] = useState(initialValue)

  return (
    <PathInput
      deleteUploadedAsset={deleteUploadedAsset}
      disabled={disabled}
      label="输入媒体路径"
      multiline
      onChange={setValue}
      onUploadPendingChange={onUploadPendingChange}
      uploadFiles={uploadFiles}
      value={value}
    />
  )
}

function ControlledCommittedPathInput({
  disabled = false,
  initialValue = '',
  multiline = false,
  onChange,
  onCommit,
  pickPaths,
  resolveDroppedFilePaths,
}: {
  disabled?: boolean
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
      disabled={disabled}
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
  it('keeps an aria-disabled picker focusable and guards all activation', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const pickPaths = vi.fn().mockResolvedValue(['E:\\media\\screen.png'])
    render(
      <PathInput
        disabled
        label="输入媒体路径"
        onChange={onChange}
        pickPaths={pickPaths}
        value=""
      />,
    )
    const picker = screen.getByRole('button', { name: '指定输入媒体路径' })

    await user.tab()

    expect(picker).toHaveFocus()
    expect(picker).not.toBeDisabled()
    expect(picker).toHaveAttribute('aria-disabled', 'true')

    await user.keyboard('{Enter}')
    await user.click(picker)

    expect(pickPaths).not.toHaveBeenCalled()
    expect(onChange).not.toHaveBeenCalled()
  })

  it('cancels a pending picker intent when disabled and permits a new pick after re-enable', async () => {
    const user = userEvent.setup()
    const firstSelection = deferred<readonly string[]>()
    const onChange = vi.fn()
    const onCommit = vi.fn()
    const pickPaths = vi
      .fn()
      .mockImplementationOnce(() => firstSelection.promise)
      .mockResolvedValueOnce(['F:\\picked\\new.png'])
    const renderInput = (disabled: boolean) => (
      <ControlledCommittedPathInput
        disabled={disabled}
        initialValue={'C:\\existing\\path.png'}
        onChange={onChange}
        onCommit={onCommit}
        pickPaths={pickPaths}
      />
    )
    const { rerender } = render(renderInput(false))
    const input = screen.getByLabelText('输入媒体路径')
    const picker = screen.getByRole('button', { name: '指定输入媒体路径' })

    await user.click(picker)
    expect(picker).toHaveAttribute('aria-disabled', 'true')

    rerender(renderInput(true))
    await act(async () => {
      firstSelection.resolve(['E:\\picked\\stale.png'])
      await firstSelection.promise
    })

    expect(input).toHaveValue('C:\\existing\\path.png')
    expect(onChange).not.toHaveBeenCalled()
    expect(onCommit).not.toHaveBeenCalled()

    rerender(renderInput(false))
    expect(picker).toHaveAttribute('aria-disabled', 'false')
    await user.click(picker)

    await waitFor(() => expect(onCommit).toHaveBeenCalledWith('F:\\picked\\new.png'))
    expect(input).toHaveValue('F:\\picked\\new.png')
    expect(onChange).toHaveBeenCalledOnce()
  })

  it('cancels a pending drop resolution when disabled and permits a new drop after re-enable', async () => {
    const firstResolution = deferred<readonly string[]>()
    const onChange = vi.fn()
    const onCommit = vi.fn()
    const resolveDroppedFilePaths = vi
      .fn()
      .mockImplementationOnce(() => firstResolution.promise)
      .mockResolvedValueOnce(['F:\\resolved\\new.png'])
    const renderInput = (disabled: boolean) => (
      <ControlledCommittedPathInput
        disabled={disabled}
        initialValue={'C:\\existing\\path.png'}
        multiline
        onChange={onChange}
        onCommit={onCommit}
        resolveDroppedFilePaths={resolveDroppedFilePaths}
      />
    )
    const { rerender } = render(renderInput(false))
    const input = screen.getByLabelText('输入媒体路径')

    fireEvent.drop(input, {
      dataTransfer: {
        files: [new File(['stale'], 'stale.png', { type: 'image/png' })],
        getData: () => '',
      },
    })
    await waitFor(() => expect(resolveDroppedFilePaths).toHaveBeenCalledOnce())

    rerender(renderInput(true))
    await act(async () => {
      firstResolution.resolve(['E:\\resolved\\stale.png'])
      await firstResolution.promise
    })

    expect(input).toHaveValue('C:\\existing\\path.png')
    expect(onChange).not.toHaveBeenCalled()
    expect(onCommit).not.toHaveBeenCalled()

    rerender(renderInput(false))
    fireEvent.drop(input, {
      dataTransfer: {
        files: [new File(['new'], 'new.png', { type: 'image/png' })],
        getData: () => '',
      },
    })

    const expectedValue = 'C:\\existing\\path.png\nF:\\resolved\\new.png'
    await waitFor(() => expect(onCommit).toHaveBeenCalledWith(expectedValue))
    expect(input).toHaveValue(expectedValue)
    expect(onChange).toHaveBeenCalledOnce()
  })

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

  it('commits once when keyboard focus tabs to the adjacent picker', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const onCommit = vi.fn()
    render(<ControlledCommittedPathInput onChange={onChange} onCommit={onCommit} />)
    const input = screen.getByLabelText('输入媒体路径')
    const picker = screen.getByRole('button', { name: '指定输入媒体路径' })

    await user.type(input, 'D:\\art\\screen.png')
    await user.tab()

    expect(picker).toHaveFocus()
    expect(onCommit).toHaveBeenCalledOnce()
    expect(onCommit).toHaveBeenCalledWith('D:\\art\\screen.png')
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
        onChange={onChange}
        onCommit={onCommit}
        pickPaths={pickPaths}
      />,
    )
    const input = screen.getByLabelText('输入媒体路径')

    await user.type(input, 'D:\\typed\\screen.png')
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
    const input = screen.getByLabelText('输入媒体路径')

    await user.type(input, 'D:\\typed\\screen.png')
    await user.click(screen.getByRole('button', { name: '指定输入媒体路径' }))

    await waitFor(() => expect(screen.getByLabelText('本地路径')).toHaveFocus())
    expect(onChange).toHaveBeenLastCalledWith('D:\\typed\\screen.png')
    expect(onCommit).not.toHaveBeenCalled()
  })

  it('merges picker paths with the latest multiline value after awaiting', async () => {
    const user = userEvent.setup()
    const selection = deferred<readonly string[]>()
    const onChange = vi.fn()
    const onCommit = vi.fn()
    const pickPaths = vi.fn(() => selection.promise)
    const { rerender } = render(
      <PathInput
        label="输入媒体路径"
        multiline
        onChange={onChange}
        onCommit={onCommit}
        pickPaths={pickPaths}
        value="C:\\media\\one.mp4"
      />,
    )

    await user.click(screen.getByRole('button', { name: '指定输入媒体路径' }))
    rerender(
      <PathInput
        label="输入媒体路径"
        multiline
        onChange={onChange}
        onCommit={onCommit}
        pickPaths={pickPaths}
        value={'C:\\media\\one.mp4\nD:\\media\\two.mp4'}
      />,
    )
    selection.resolve(['E:\\media\\three.mp4'])

    await waitFor(() =>
      expect(onCommit).toHaveBeenCalledWith(
        'C:\\media\\one.mp4\nD:\\media\\two.mp4\nE:\\media\\three.mp4',
      ),
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
    const { rerender } = render(
      <PathInput
        label="输入媒体路径"
        multiline
        onChange={onChange}
        onCommit={onCommit}
        resolveDroppedFilePaths={resolveDroppedFilePaths}
        value="C:\\media\\one.mp4"
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
    rerender(
      <PathInput
        label="输入媒体路径"
        multiline
        onChange={onChange}
        onCommit={onCommit}
        resolveDroppedFilePaths={resolveDroppedFilePaths}
        value={'C:\\media\\one.mp4\nD:\\media\\two.mp4'}
      />,
    )
    resolution.resolve(['E:\\media\\three.mp4'])

    await waitFor(() =>
      expect(onCommit).toHaveBeenCalledWith(
        'C:\\media\\one.mp4\nD:\\media\\two.mp4\nE:\\media\\three.mp4',
      ),
    )
  })

  it('ignores a resolved drop after a newer typed path is committed', async () => {
    const user = userEvent.setup()
    const resolution = deferred<readonly string[]>()
    const onChange = vi.fn()
    const onCommit = vi.fn()
    const resolveDroppedFilePaths = vi.fn(() => resolution.promise)
    render(
      <ControlledCommittedPathInput
        onChange={onChange}
        onCommit={onCommit}
        resolveDroppedFilePaths={resolveDroppedFilePaths}
      />,
    )
    const input = screen.getByLabelText('输入媒体路径')

    fireEvent.drop(input, {
      dataTransfer: {
        files: [new File(['image'], 'old.png', { type: 'image/png' })],
        getData: () => '',
      },
    })
    await waitFor(() => expect(resolveDroppedFilePaths).toHaveBeenCalledOnce())

    await user.type(input, 'D:\\typed\\new.png')
    await user.keyboard('{Enter}')
    expect(onCommit).toHaveBeenCalledOnce()
    expect(onCommit).toHaveBeenLastCalledWith('D:\\typed\\new.png')

    const changeCount = onChange.mock.calls.length
    await act(async () => {
      resolution.resolve(['E:\\resolved\\old.png'])
      await resolution.promise
    })

    expect(input).toHaveValue('D:\\typed\\new.png')
    expect(onChange).toHaveBeenCalledTimes(changeCount)
    expect(onCommit).toHaveBeenCalledOnce()
  })

  it('ignores a resolved drop after Enter commits the current explicit path', async () => {
    const user = userEvent.setup()
    const resolution = deferred<readonly string[]>()
    const onChange = vi.fn()
    const onCommit = vi.fn()
    const resolveDroppedFilePaths = vi.fn(() => resolution.promise)
    render(
      <ControlledCommittedPathInput
        initialValue={'D:\\explicit\\current.png'}
        onChange={onChange}
        onCommit={onCommit}
        resolveDroppedFilePaths={resolveDroppedFilePaths}
      />,
    )
    const input = screen.getByLabelText('输入媒体路径')

    fireEvent.drop(input, {
      dataTransfer: {
        files: [new File(['image'], 'old.png', { type: 'image/png' })],
        getData: () => '',
      },
    })
    await waitFor(() => expect(resolveDroppedFilePaths).toHaveBeenCalledOnce())

    await user.click(input)
    await user.keyboard('{Enter}')
    expect(onChange).not.toHaveBeenCalled()
    expect(onCommit).toHaveBeenCalledOnce()
    expect(onCommit).toHaveBeenCalledWith('D:\\explicit\\current.png')

    await act(async () => {
      resolution.resolve(['E:\\resolved\\old.png'])
      await resolution.promise
    })

    expect(input).toHaveValue('D:\\explicit\\current.png')
    expect(onChange).not.toHaveBeenCalled()
    expect(onCommit).toHaveBeenCalledOnce()
  })

  it('ignores a resolved drop after blur commits the current explicit path', async () => {
    const resolution = deferred<readonly string[]>()
    const onChange = vi.fn()
    const onCommit = vi.fn()
    const resolveDroppedFilePaths = vi.fn(() => resolution.promise)
    render(
      <ControlledCommittedPathInput
        initialValue={'D:\\explicit\\current.png'}
        onChange={onChange}
        onCommit={onCommit}
        resolveDroppedFilePaths={resolveDroppedFilePaths}
      />,
    )
    const input = screen.getByLabelText('输入媒体路径')

    fireEvent.drop(input, {
      dataTransfer: {
        files: [new File(['image'], 'old.png', { type: 'image/png' })],
        getData: () => '',
      },
    })
    await waitFor(() => expect(resolveDroppedFilePaths).toHaveBeenCalledOnce())

    fireEvent.focus(input)
    fireEvent.blur(input)
    expect(onChange).not.toHaveBeenCalled()
    expect(onCommit).toHaveBeenCalledOnce()
    expect(onCommit).toHaveBeenCalledWith('D:\\explicit\\current.png')

    await act(async () => {
      resolution.resolve(['E:\\resolved\\old.png'])
      await resolution.promise
    })

    expect(input).toHaveValue('D:\\explicit\\current.png')
    expect(onChange).not.toHaveBeenCalled()
    expect(onCommit).toHaveBeenCalledOnce()
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

  it('keeps a pending fallback editor disabled without committing after the prop changes', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const onCommit = vi.fn()
    const pickPaths = vi.fn().mockRejectedValue(new Error('Native picker unavailable'))
    const renderInput = (disabled: boolean) => (
      <PathInput
        disabled={disabled}
        label="输入媒体路径"
        onChange={onChange}
        onCommit={onCommit}
        pickPaths={pickPaths}
        value="C:\\old\\screen.png"
      />
    )
    const { rerender } = render(renderInput(false))

    await user.click(screen.getByRole('button', { name: '指定输入媒体路径' }))
    const editor = await screen.findByLabelText('本地路径')
    await user.clear(editor)
    await user.type(editor, 'D:\\edited\\screen.png')

    rerender(renderInput(true))

    const apply = screen.getByRole('button', { name: '应用路径' })
    expect(screen.getByRole('dialog', { name: '指定输入媒体路径' })).toBeInTheDocument()
    expect(editor).toBeDisabled()
    expect(editor).toHaveValue('D:\\edited\\screen.png')
    expect(apply).toBeDisabled()

    fireEvent.change(editor, { target: { value: 'E:\\blocked\\screen.png' } })
    fireEvent.click(apply)

    expect(onChange).not.toHaveBeenCalled()
    expect(onCommit).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog', { name: '指定输入媒体路径' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '取消' }))
    expect(screen.queryByRole('dialog', { name: '指定输入媒体路径' })).not.toBeInTheDocument()
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

  it('uploads browser files and displays their original names', async () => {
    const user = userEvent.setup()
    const uploadFiles = vi.fn(async (_files, onProgress) => {
      onProgress(50)
      onProgress(100)
      return ['asset:11111111111111111111111111111111:screen.png']
    })
    const { container } = render(
      <ControlledUploadPathInput uploadFiles={uploadFiles} />,
    )
    const fileInput = container.querySelector<HTMLInputElement>(
      'input[type="file"]',
    )
    if (!fileInput) {
      throw new Error('upload input missing')
    }

    await user.upload(
      fileInput,
      new File(['image'], 'screen.png', { type: 'image/png' }),
    )

    await waitFor(() =>
      expect(screen.getByLabelText('输入媒体路径')).toHaveValue('screen.png'),
    )
    expect(uploadFiles).toHaveBeenCalledOnce()
  })

  it('displays the decoded final filename segment of four-part references', () => {
    render(
      <ControlledUploadPathInput
        initialValue={`asset:${'1'.repeat(32)}:${'2'.repeat(32)}:${encodeURIComponent('素材:100%.png')}`}
        uploadFiles={vi.fn()}
      />,
    )

    expect(screen.getByLabelText('输入媒体路径')).toHaveValue('素材:100%.png')
  })

  it('commits replacements before deleting only unretained old references', async () => {
    const retained = `asset:${'1'.repeat(32)}:${'2'.repeat(32)}:retained.png`
    const removed = `asset:${'3'.repeat(32)}:${'4'.repeat(32)}:old.png`
    const added = `asset:${'5'.repeat(32)}:${'6'.repeat(32)}:new.png`
    const deleteUploadedAsset = vi.fn().mockResolvedValue(undefined)
    const uploadFiles = vi.fn().mockResolvedValue([retained, added])
    const { container } = render(
      <ControlledUploadPathInput
        deleteUploadedAsset={deleteUploadedAsset}
        initialValue={`${removed}\n${retained}`}
        uploadFiles={uploadFiles}
      />,
    )
    const fileInput = container.querySelector<HTMLInputElement>('input[type="file"]')
    if (!fileInput) {
      throw new Error('upload input missing')
    }

    fireEvent.change(fileInput, {
      target: {
        files: [
          new File(['retained'], 'retained.png', { type: 'image/png' }),
          new File(['new'], 'new.png', { type: 'image/png' }),
        ],
      },
    })

    await waitFor(() =>
      expect(screen.getByLabelText('输入媒体路径')).toHaveValue(
        'retained.png\nnew.png',
      ),
    )
    expect(deleteUploadedAsset).toHaveBeenCalledOnce()
    expect(deleteUploadedAsset).toHaveBeenCalledWith(removed)
  })

  it('keeps the committed replacement visible when old asset cleanup fails', async () => {
    const previous = `asset:${'1'.repeat(32)}:${'2'.repeat(32)}:old.png`
    const replacement = `asset:${'3'.repeat(32)}:${'4'.repeat(32)}:new.png`
    const deleteUploadedAsset = vi
      .fn()
      .mockRejectedValue(new Error('旧文件清理失败'))
    const uploadFiles = vi.fn().mockResolvedValue([replacement])
    const { container } = render(
      <ControlledUploadPathInput
        deleteUploadedAsset={deleteUploadedAsset}
        initialValue={previous}
        uploadFiles={uploadFiles}
      />,
    )
    const fileInput = container.querySelector<HTMLInputElement>('input[type="file"]')
    if (!fileInput) {
      throw new Error('upload input missing')
    }

    fireEvent.change(fileInput, {
      target: { files: [new File(['new'], 'new.png', { type: 'image/png' })] },
    })

    expect(await screen.findByText('旧文件清理失败')).toBeInTheDocument()
    expect(screen.getByLabelText('输入媒体路径')).toHaveValue('new.png')
    expect(deleteUploadedAsset).toHaveBeenCalledWith(previous)
  })

  it('reports replacement cleanup failure after the committed value disables the input', async () => {
    const previous = `asset:${'1'.repeat(32)}:${'2'.repeat(32)}:old.png`
    const replacement = `asset:${'3'.repeat(32)}:${'4'.repeat(32)}:new.png`
    const cleanup = deferred<void>()
    const deleteUploadedAsset = vi.fn(() => cleanup.promise)
    const uploadFiles = vi.fn().mockResolvedValue([replacement])
    const renderInput = (disabled: boolean) => (
      <ControlledUploadPathInput
        deleteUploadedAsset={deleteUploadedAsset}
        disabled={disabled}
        initialValue={previous}
        uploadFiles={uploadFiles}
      />
    )
    const { container, rerender } = render(renderInput(false))
    const fileInput = container.querySelector<HTMLInputElement>('input[type="file"]')
    if (!fileInput) {
      throw new Error('upload input missing')
    }

    fireEvent.change(fileInput, {
      target: { files: [new File(['new'], 'new.png', { type: 'image/png' })] },
    })
    await waitFor(() => expect(deleteUploadedAsset).toHaveBeenCalledWith(previous))
    rerender(renderInput(true))

    await act(async () => {
      cleanup.reject(new Error('旧文件清理失败'))
      await cleanup.promise.catch(() => undefined)
    })

    expect(screen.getByText('旧文件清理失败')).toBeInTheDocument()
    expect(screen.getByLabelText('输入媒体路径')).toHaveValue('new.png')
  })

  it('retains rejected cleanup after unmount and retries it on the next upload', async () => {
    const previous = `asset:${'1'.repeat(32)}:${'2'.repeat(32)}:old.png`
    const replacement = `asset:${'3'.repeat(32)}:${'4'.repeat(32)}:new.png`
    const firstCleanup = deferred<Response>()
    const cleanupError = new Error('旧文件清理失败')
    const cleanup = vi
      .spyOn(globalThis, 'fetch')
      .mockReturnValueOnce(firstCleanup.promise)
      .mockRejectedValueOnce(cleanupError)
      .mockRejectedValueOnce(cleanupError)
      .mockResolvedValue(new Response(null, { status: 204 }))
    const firstUpload = vi.fn().mockResolvedValue([replacement])
    const firstRender = render(
      <ControlledUploadPathInput
        deleteUploadedAsset={deleteUploadedAssetWithQueue}
        initialValue={previous}
        uploadFiles={firstUpload}
      />,
    )
    const firstFileInput = firstRender.container.querySelector<HTMLInputElement>(
      'input[type="file"]',
    )
    if (!firstFileInput) {
      throw new Error('upload input missing')
    }

    fireEvent.change(firstFileInput, {
      target: { files: [new File(['new'], 'new.png', { type: 'image/png' })] },
    })
    await waitFor(() => expect(cleanup).toHaveBeenCalledOnce())
    firstRender.unmount()
    await act(async () => {
      firstCleanup.reject(cleanupError)
      await firstCleanup.promise.catch(() => undefined)
    })
    await waitFor(() => expect(cleanup).toHaveBeenCalledTimes(3))

    const requests: MockUploadRequest[] = []
    class MockUploadRequest {
      status = 201
      response: unknown = null
      listeners = new Map<string, () => void>()
      upload = {
        addEventListener: () => undefined,
      }

      constructor() {
        requests.push(this)
      }

      open() {}

      setRequestHeader() {}

      addEventListener(type: string, listener: () => void) {
        this.listeners.set(type, listener)
      }

      send() {}
    }
    vi.stubGlobal('XMLHttpRequest', MockUploadRequest)
    vi.spyOn(globalThis.crypto, 'randomUUID')
      .mockReturnValueOnce('55555555-5555-5555-5555-555555555555')
      .mockReturnValueOnce('66666666-6666-6666-6666-666666666666')
    const secondRender = render(
      <ControlledUploadPathInput uploadFiles={uploadFilesWithQueue} />,
    )
    const secondFileInput =
      secondRender.container.querySelector<HTMLInputElement>('input[type="file"]')
    if (!secondFileInput) {
      throw new Error('upload input missing')
    }

    fireEvent.change(secondFileInput, {
      target: { files: [new File(['next'], 'next.png', { type: 'image/png' })] },
    })
    await waitFor(() => expect(requests).toHaveLength(1))
    await act(async () => {
      requests[0].response = {
        filename: 'next.png',
        reference: `asset:${'5'.repeat(32)}:${'6'.repeat(32)}:next.png`,
        size: 4,
      }
      requests[0].listeners.get('load')?.()
    })

    await waitFor(() =>
      expect(screen.getByLabelText('输入媒体路径')).toHaveValue('next.png'),
    )
    expect(cleanup).toHaveBeenLastCalledWith(
      `/api/uploads/${'1'.repeat(32)}?generation=${'2'.repeat(32)}`,
      { method: 'DELETE' },
    )
  })

  it('leaves failed cleanup retry ownership outside the component', async () => {
    const previous = `asset:${'1'.repeat(32)}:${'2'.repeat(32)}:old.png`
    const firstReplacement = `asset:${'3'.repeat(32)}:${'4'.repeat(32)}:first.png`
    const secondReplacement = `asset:${'5'.repeat(32)}:${'6'.repeat(32)}:second.png`
    const deleteUploadedAsset = vi
      .fn()
      .mockRejectedValueOnce(new Error('旧文件清理失败'))
      .mockResolvedValue(undefined)
    const uploadFiles = vi
      .fn()
      .mockResolvedValueOnce([firstReplacement])
      .mockResolvedValueOnce([secondReplacement])
    const { container } = render(
      <ControlledUploadPathInput
        deleteUploadedAsset={deleteUploadedAsset}
        initialValue={previous}
        uploadFiles={uploadFiles}
      />,
    )
    const fileInput = container.querySelector<HTMLInputElement>('input[type="file"]')
    if (!fileInput) {
      throw new Error('upload input missing')
    }

    fireEvent.change(fileInput, {
      target: { files: [new File(['first'], 'first.png', { type: 'image/png' })] },
    })
    expect(await screen.findByText('旧文件清理失败')).toBeInTheDocument()

    fireEvent.change(fileInput, {
      target: { files: [new File(['second'], 'second.png', { type: 'image/png' })] },
    })

    await waitFor(() => {
      expect(deleteUploadedAsset).toHaveBeenCalledTimes(2)
    })
    expect(deleteUploadedAsset.mock.calls).toEqual([
      [previous],
      [firstReplacement],
    ])
    expect(screen.getByLabelText('输入媒体路径')).toHaveValue('second.png')
    expect(screen.queryByText('旧文件清理失败')).not.toBeInTheDocument()
  })

  it('does not let stale cleanup failure overwrite a newer upload error', async () => {
    const previous = `asset:${'1'.repeat(32)}:${'2'.repeat(32)}:old.png`
    const replacement = `asset:${'3'.repeat(32)}:${'4'.repeat(32)}:new.png`
    const cleanup = deferred<void>()
    const deleteUploadedAsset = vi.fn(() => cleanup.promise)
    const uploadFiles = vi
      .fn()
      .mockResolvedValueOnce([replacement])
      .mockRejectedValueOnce(new Error('新上传失败'))
    const { container } = render(
      <ControlledUploadPathInput
        deleteUploadedAsset={deleteUploadedAsset}
        initialValue={previous}
        uploadFiles={uploadFiles}
      />,
    )
    const fileInput = container.querySelector<HTMLInputElement>('input[type="file"]')
    if (!fileInput) {
      throw new Error('upload input missing')
    }

    fireEvent.change(fileInput, {
      target: { files: [new File(['first'], 'first.png', { type: 'image/png' })] },
    })
    await waitFor(() => expect(deleteUploadedAsset).toHaveBeenCalledWith(previous))
    fireEvent.change(fileInput, {
      target: { files: [new File(['second'], 'second.png', { type: 'image/png' })] },
    })
    expect(await screen.findByText('新上传失败')).toBeInTheDocument()

    await act(async () => {
      cleanup.reject(new Error('过期清理失败'))
      await cleanup.promise.catch(() => undefined)
    })

    expect(screen.getByText('新上传失败')).toBeInTheDocument()
    expect(screen.queryByText('过期清理失败')).not.toBeInTheDocument()
    expect(screen.getByLabelText('输入媒体路径')).toHaveValue('new.png')
  })

  it('reports upload pending until a replacement upload resolves', async () => {
    const uploadRequest = deferred<readonly string[]>()
    const uploadFiles = vi.fn(() => uploadRequest.promise)
    const onUploadPendingChange = vi.fn()
    const { container } = render(
      <ControlledUploadPathInput
        initialValue="asset:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:old.png"
        onUploadPendingChange={onUploadPendingChange}
        uploadFiles={uploadFiles}
      />,
    )
    const fileInput = container.querySelector<HTMLInputElement>('input[type="file"]')
    if (!fileInput) {
      throw new Error('upload input missing')
    }

    fireEvent.change(fileInput, {
      target: { files: [new File(['new'], 'new.png', { type: 'image/png' })] },
    })

    expect(onUploadPendingChange).toHaveBeenCalledTimes(1)
    expect(onUploadPendingChange).toHaveBeenLastCalledWith(true)
    expect(screen.getByLabelText('输入媒体路径')).toHaveValue('old.png')

    await act(async () => {
      uploadRequest.resolve(['asset:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:new.png'])
      await uploadRequest.promise
    })

    expect(screen.getByLabelText('输入媒体路径')).toHaveValue('new.png')
    expect(onUploadPendingChange.mock.calls).toEqual([[true], [false]])
  })

  it('reports upload completion after rejection while preserving the previous value', async () => {
    const uploadFiles = vi.fn().mockRejectedValue(new Error('上传失败'))
    const onUploadPendingChange = vi.fn()
    const { container } = render(
      <ControlledUploadPathInput
        initialValue="asset:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:old.png"
        onUploadPendingChange={onUploadPendingChange}
        uploadFiles={uploadFiles}
      />,
    )
    const fileInput = container.querySelector<HTMLInputElement>('input[type="file"]')
    if (!fileInput) {
      throw new Error('upload input missing')
    }

    fireEvent.change(fileInput, {
      target: { files: [new File(['new'], 'new.png', { type: 'image/png' })] },
    })

    expect(onUploadPendingChange).toHaveBeenCalledWith(true)
    expect(await screen.findByText('上传失败')).toBeInTheDocument()
    expect(screen.getByLabelText('输入媒体路径')).toHaveValue('old.png')
    expect(onUploadPendingChange.mock.calls).toEqual([[true], [false]])
  })

  it('keeps a newer upload pending when a stale aborted upload settles', async () => {
    const firstRequest = deferred<readonly string[]>()
    const secondRequest = deferred<readonly string[]>()
    const uploadFiles = vi
      .fn()
      .mockReturnValueOnce(firstRequest.promise)
      .mockReturnValueOnce(secondRequest.promise)
    const deleteUploadedAsset = vi.fn().mockResolvedValue(undefined)
    const onUploadPendingChange = vi.fn()
    const renderInput = (disabled: boolean) => (
      <ControlledUploadPathInput
        deleteUploadedAsset={deleteUploadedAsset}
        disabled={disabled}
        initialValue="asset:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:old.png"
        onUploadPendingChange={onUploadPendingChange}
        uploadFiles={uploadFiles}
      />
    )
    const { container, rerender } = render(renderInput(false))
    const fileInput = container.querySelector<HTMLInputElement>('input[type="file"]')
    if (!fileInput) {
      throw new Error('upload input missing')
    }

    fireEvent.change(fileInput, {
      target: { files: [new File(['first'], 'first.png', { type: 'image/png' })] },
    })
    rerender(renderInput(true))
    expect(onUploadPendingChange.mock.calls).toEqual([[true], [false]])

    rerender(renderInput(false))
    fireEvent.change(fileInput, {
      target: { files: [new File(['second'], 'second.png', { type: 'image/png' })] },
    })
    expect(onUploadPendingChange.mock.calls).toEqual([[true], [false], [true]])

    await act(async () => {
      firstRequest.resolve(['asset:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:first.png'])
      await firstRequest.promise
    })
    expect(onUploadPendingChange.mock.calls).toEqual([[true], [false], [true]])
    expect(screen.getByLabelText('输入媒体路径')).toHaveValue('old.png')
    expect(deleteUploadedAsset).not.toHaveBeenCalled()

    await act(async () => {
      secondRequest.resolve(['asset:cccccccccccccccccccccccccccccccc:second.png'])
      await secondRequest.promise
    })
    expect(onUploadPendingChange.mock.calls).toEqual([[true], [false], [true], [false]])
    expect(screen.getByLabelText('输入媒体路径')).toHaveValue('second.png')
    expect(deleteUploadedAsset).toHaveBeenCalledOnce()
    expect(deleteUploadedAsset).toHaveBeenCalledWith(
      'asset:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:old.png',
    )
  })

  it('keeps the active upload intent when another file is dropped', async () => {
    const user = userEvent.setup()
    const uploadRequest = deferred<readonly string[]>()
    const uploadFiles = vi.fn(() => uploadRequest.promise)
    const { container } = render(
      <ControlledUploadPathInput uploadFiles={uploadFiles} />,
    )
    const input = screen.getByLabelText('输入媒体路径')
    const picker = screen.getByRole('button', { name: '上传输入媒体路径' })
    const fileInput = container.querySelector<HTMLInputElement>(
      'input[type="file"]',
    )
    if (!fileInput) {
      throw new Error('upload input missing')
    }
    const fileInputClick = vi.spyOn(fileInput, 'click')

    fireEvent.change(fileInput, {
      target: { files: [new File(['first'], 'first.png', { type: 'image/png' })] },
    })
    expect(screen.getByLabelText('上传进度')).toHaveValue(0)
    expect(picker).toHaveAttribute('aria-disabled', 'true')

    fireEvent.drop(input, {
      dataTransfer: {
        files: [new File(['second'], 'second.png', { type: 'image/png' })],
        getData: () => '',
      },
    })
    picker.focus()
    await user.keyboard('{Enter}')

    expect(uploadFiles).toHaveBeenCalledOnce()
    expect(fileInputClick).not.toHaveBeenCalled()

    await act(async () => {
      uploadRequest.resolve([
        'asset:11111111111111111111111111111111:first.png',
      ])
      await uploadRequest.promise
    })

    expect(input).toHaveValue('first.png')
    expect(screen.queryByLabelText('上传进度')).not.toBeInTheDocument()
    expect(picker).toHaveAttribute('aria-disabled', 'false')
  })

  it('starts only one upload for rapid consecutive file selections', async () => {
    const uploadRequest = deferred<readonly string[]>()
    const uploadFiles = vi.fn(() => uploadRequest.promise)
    const { container } = render(
      <ControlledUploadPathInput uploadFiles={uploadFiles} />,
    )
    const fileInput = container.querySelector<HTMLInputElement>(
      'input[type="file"]',
    )
    if (!fileInput) {
      throw new Error('upload input missing')
    }

    fireEvent.change(fileInput, {
      target: { files: [new File(['first'], 'first.png', { type: 'image/png' })] },
    })
    expect(fileInput).toBeDisabled()
    fireEvent.change(fileInput, {
      target: { files: [new File(['second'], 'second.png', { type: 'image/png' })] },
    })

    expect(uploadFiles).toHaveBeenCalledOnce()

    await act(async () => {
      uploadRequest.resolve([
        'asset:11111111111111111111111111111111:first.png',
      ])
      await uploadRequest.promise
    })

    expect(screen.getByLabelText('输入媒体路径')).toHaveValue('first.png')
    expect(screen.queryByLabelText('上传进度')).not.toBeInTheDocument()
  })

  it('aborts an in-flight browser upload when unmounted', async () => {
    let uploadSignal: AbortSignal | undefined
    const uploadRequest = deferred<readonly string[]>()
    const onUploadPendingChange = vi.fn()
    const deleteUploadedAsset = vi.fn().mockResolvedValue(undefined)
    const uploadFiles = vi.fn(
      (
        _files: readonly File[],
        _onProgress: (percentage: number) => void,
        signal?: AbortSignal,
      ) => {
        uploadSignal = signal
        return uploadRequest.promise
      },
    )
    const { container, unmount } = render(
      <ControlledUploadPathInput
        deleteUploadedAsset={deleteUploadedAsset}
        initialValue={`asset:${'1'.repeat(32)}:${'2'.repeat(32)}:current.png`}
        onUploadPendingChange={onUploadPendingChange}
        uploadFiles={uploadFiles}
      />,
    )
    const fileInput = container.querySelector<HTMLInputElement>(
      'input[type="file"]',
    )
    if (!fileInput) {
      throw new Error('upload input missing')
    }

    fireEvent.change(fileInput, {
      target: { files: [new File(['image'], 'screen.png', { type: 'image/png' })] },
    })
    expect(uploadSignal?.aborted).toBe(false)
    expect(onUploadPendingChange).toHaveBeenLastCalledWith(true)

    unmount()

    expect(uploadSignal?.aborted).toBe(true)
    expect(onUploadPendingChange.mock.calls).toEqual([[true], [false]])
    await act(async () => {
      uploadRequest.resolve([
        `asset:${'3'.repeat(32)}:${'4'.repeat(32)}:stale.png`,
      ])
      await uploadRequest.promise
    })
    expect(onUploadPendingChange.mock.calls).toEqual([[true], [false]])
    expect(deleteUploadedAsset).not.toHaveBeenCalled()
  })

  it('ignores stale upload progress after its intent is invalidated', async () => {
    let reportProgress: ((percentage: number) => void) | undefined
    let uploadSignal: AbortSignal | undefined
    const uploadRequest = deferred<readonly string[]>()
    const uploadFiles = vi.fn(
      (
        _files: readonly File[],
        onProgress: (percentage: number) => void,
        signal?: AbortSignal,
      ) => {
        reportProgress = onProgress
        uploadSignal = signal
        return uploadRequest.promise
      },
    )
    const renderInput = (disabled: boolean) => (
      <PathInput
        disabled={disabled}
        label="输入媒体路径"
        onChange={() => undefined}
        uploadFiles={uploadFiles}
        value=""
      />
    )
    const { container, rerender } = render(renderInput(false))
    const fileInput = container.querySelector<HTMLInputElement>(
      'input[type="file"]',
    )
    if (!fileInput) {
      throw new Error('upload input missing')
    }

    fireEvent.change(fileInput, {
      target: { files: [new File(['image'], 'screen.png', { type: 'image/png' })] },
    })
    expect(screen.getByLabelText('上传进度')).toHaveValue(0)

    rerender(renderInput(true))
    rerender(renderInput(false))
    act(() => reportProgress?.(75))

    expect(uploadSignal?.aborted).toBe(true)
    expect(screen.queryByLabelText('上传进度')).not.toBeInTheDocument()
    await act(async () => {
      uploadRequest.resolve([])
      await uploadRequest.promise
    })
    expect(screen.queryByLabelText('上传进度')).not.toBeInTheDocument()
  })
})
