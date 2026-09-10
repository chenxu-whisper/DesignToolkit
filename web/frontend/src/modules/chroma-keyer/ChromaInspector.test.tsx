import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useRef, useState } from 'react'
import { describe, expect, it } from 'vitest'
import { ChromaInspector } from './ChromaInspector'

function InspectorHarness({ compact }: { compact: boolean }) {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        ref={triggerRef}
        type="button"
      >
        打开参数检查器
      </button>
      <ChromaInspector
        compact={compact}
        onOpenChange={setOpen}
        open={open}
        triggerRef={triggerRef}
      >
        <label>
          Screen Gain
          <input type="number" />
        </label>
      </ChromaInspector>
    </>
  )
}

describe('ChromaInspector', () => {
  it('renders as a persistent complementary region on wide screens', () => {
    render(<InspectorHarness compact={false} />)

    expect(screen.getByRole('complementary', { name: '色键参数' })).toBeInTheDocument()
    expect(screen.getByLabelText('Screen Gain')).toBeInTheDocument()
  })

  it('opens as a dialog on compact screens', async () => {
    const user = userEvent.setup()
    render(<InspectorHarness compact />)

    expect(screen.queryByRole('dialog', { name: '色键参数' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '打开参数检查器' }))

    expect(screen.getByRole('dialog', { name: '色键参数' })).toHaveAttribute('open')
  })

  it('restores focus to the trigger after closing the compact dialog', async () => {
    const user = userEvent.setup()
    render(<InspectorHarness compact />)
    const trigger = screen.getByRole('button', { name: '打开参数检查器' })

    await user.click(trigger)
    await user.click(screen.getByRole('button', { name: '关闭参数检查器' }))

    expect(screen.queryByRole('dialog', { name: '色键参数' })).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('does not reopen after leaving and returning to compact layout', async () => {
    const user = userEvent.setup()
    const { rerender } = render(<InspectorHarness compact />)

    await user.click(screen.getByRole('button', { name: '打开参数检查器' }))
    expect(screen.getByRole('dialog', { name: '色键参数' })).toBeInTheDocument()

    rerender(<InspectorHarness compact={false} />)
    expect(screen.getByRole('complementary', { name: '色键参数' })).toBeInTheDocument()
    rerender(<InspectorHarness compact />)

    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: '色键参数' })).not.toBeInTheDocument()
    })
  })

  it('closes when the compact dialog backdrop is clicked', async () => {
    const user = userEvent.setup()
    render(<InspectorHarness compact />)
    const trigger = screen.getByRole('button', { name: '打开参数检查器' })

    await user.click(trigger)
    fireEvent.click(screen.getByRole('dialog', { name: '色键参数' }))

    expect(screen.queryByRole('dialog', { name: '色键参数' })).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })
})
