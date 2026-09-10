import { Film, Pipette, Scaling } from 'lucide-react'
import { ChromaKeyerPage } from '../modules/chroma-keyer/ChromaKeyerPage'
import { MediaResizePage } from '../modules/media-resize/MediaResizePage'
import { VideoFramesPage } from '../modules/video-frames/VideoFramesPage'
import type { ToolModule } from './types'

export const toolModules: ToolModule[] = [
  {
    component: VideoFramesPage,
    description: '将视频转换为连续图像帧',
    icon: Film,
    id: 'video_frames',
    label: '视频转序列',
  },
  {
    component: MediaResizePage,
    description: '批量调整图片和视频尺寸',
    icon: Scaling,
    id: 'media_resize',
    label: '媒体尺寸修改',
  },
  {
    component: ChromaKeyerPage,
    description: '预览、取色并导出透明素材',
    icon: Pipette,
    id: 'chroma_keyer',
    label: '色键抠图',
  },
]
