import { X } from 'lucide-react'
import {
  type PropsWithChildren,
  type RefObject,
  useEffect,
  useRef,
} from 'react'

type ChromaInspectorProps = PropsWithChildren<{
  compact: boolean
  onOpenChange: (open: boolean) => void
  open: boolean
  triggerRef: RefObject<HTMLButtonElement | null>
}>

export function ChromaInspector({
  children,
  compact,
  onOpenChange,
  open,
  triggerRef,
}: ChromaInspectorProps) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const wasOpenRef = useRef(false)

  useEffect(() => {
    if (!compact) {
      if (open) {
        onOpenChange(false)
      }
      wasOpenRef.current = false
      return
    }

    const dialog = dialogRef.current
    if (!dialog) {
      return
    }

    if (open) {
      wasOpenRef.current = true
      if (!dialog.open) {
        if (typeof dialog.showModal === 'function') {
          dialog.showModal()
        } else {
          dialog.setAttribute('open', '')
        }
      }
      return
    }

    if (dialog.open) {
      if (typeof dialog.close === 'function') {
        dialog.close()
      } else {
        dialog.removeAttribute('open')
      }
    }
    if (wasOpenRef.current) {
      wasOpenRef.current = false
      triggerRef.current?.focus()
    }
  }, [compact, onOpenChange, open, triggerRef])

  if (!compact) {
    return (
      <aside aria-label="色键参数" className="chroma-inspector">
        {children}
      </aside>
    )
  }

  return (
    <dialog
      aria-labelledby="chroma-inspector-title"
      className="chroma-inspector chroma-inspector-dialog"
      onCancel={(event) => {
        event.preventDefault()
        onOpenChange(false)
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          onOpenChange(false)
        }
      }}
      onClose={() => onOpenChange(false)}
      ref={dialogRef}
    >
      <div className="chroma-inspector-heading">
        <h2 id="chroma-inspector-title">色键参数</h2>
        <button
          aria-label="关闭参数检查器"
          className="icon-button"
          onClick={() => onOpenChange(false)}
          title="关闭参数检查器"
          type="button"
        >
          <X aria-hidden="true" size={18} />
        </button>
      </div>
      {children}
    </dialog>
  )
}
