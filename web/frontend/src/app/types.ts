import type { ComponentType } from 'react'

export type ModuleId = 'video_frames' | 'media_resize' | 'chroma_keyer'

export type ToolModule = {
  id: ModuleId
  label: string
  component: ComponentType
}
