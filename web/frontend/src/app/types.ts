import type { ComponentType } from 'react'
import type { LucideIcon } from 'lucide-react'

export type ModuleId = 'video_frames' | 'media_resize' | 'chroma_keyer'

export type ToolModule = {
  component: ComponentType
  description: string
  icon: LucideIcon
  id: ModuleId
  label: string
}
