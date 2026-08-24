import { MediaResizePage } from '../modules/media-resize/MediaResizePage'
import { VideoFramesPage } from '../modules/video-frames/VideoFramesPage'
import type { ToolModule } from './types'

export const toolModules: ToolModule[] = [
  { id: 'video_frames', label: '视频转序列', component: VideoFramesPage },
  { id: 'media_resize', label: '媒体尺寸修改', component: MediaResizePage },
]
