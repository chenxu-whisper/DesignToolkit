import {
  Maximize,
  Minus,
  Plus,
  Scan,
  type LucideIcon,
} from 'lucide-react'

type ImageViewportToolbarProps = {
  canZoomIn: boolean
  canZoomOut: boolean
  onActualSize: () => void
  onFit: () => void
  onZoomIn: () => void
  onZoomOut: () => void
  scale: number
}

export function ImageViewportToolbar({
  canZoomIn,
  canZoomOut,
  onActualSize,
  onFit,
  onZoomIn,
  onZoomOut,
  scale,
}: ImageViewportToolbarProps) {
  return (
    <div
      aria-label="预览缩放控制"
      className="image-viewport-toolbar"
      role="toolbar"
    >
      <ToolbarButton
        disabled={!canZoomOut}
        icon={Minus}
        label="缩小预览"
        onClick={onZoomOut}
      />
      <output
        aria-live="polite"
        className="image-viewport-zoom-status"
        role="status"
      >
        {Math.round(scale * 100)}%
      </output>
      <ToolbarButton
        disabled={!canZoomIn}
        icon={Plus}
        label="放大预览"
        onClick={onZoomIn}
      />
      <ToolbarButton icon={Scan} label="适应窗口" onClick={onFit} />
      <ToolbarButton icon={Maximize} label="实际大小" onClick={onActualSize} />
    </div>
  )
}

function ToolbarButton({
  disabled,
  icon: Icon,
  label,
  onClick,
}: {
  disabled?: boolean
  icon: LucideIcon
  label: string
  onClick: () => void
}) {
  return (
    <button
      aria-label={label}
      className="icon-button"
      disabled={disabled}
      onClick={onClick}
      title={label}
      type="button"
    >
      <Icon aria-hidden="true" size={18} />
    </button>
  )
}
