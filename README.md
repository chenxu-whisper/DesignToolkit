# DesignToolkit

DesignToolkit 是一款设计工具集，为设计工作提供便捷实用的工具支持。

## 环境要求

- Python 3.12
- uv

## 初始化环境

```powershell
uv sync --link-mode=copy
```

`--link-mode=copy` 用于规避 uv 缓存目录和项目虚拟环境位于不同文件系统，或当前环境限制硬链接行为时可能出现的 hardlink 问题。

## 运行应用

```powershell
uv run designtoolkit
```

### 视频转序列帧

默认导出：

```powershell
uv run designtoolkit video-to-frames .\demo.mp4
```

默认行为：

- 输出目录：视频同级目录下的 `demo_frames`
- 输出格式：`png`
- 透明支持：带透明通道的 WebM 导出为 PNG 序列时，会默认保留 alpha 通道；导出为 JPG/JPEG 时不保留透明信息
- 输出尺寸：原视频尺寸
- 输出帧率：原视频帧率

指定输出参数：

```powershell
uv run designtoolkit video-to-frames .\demo.mov --format jpg --output-dir .\frames --size 1920x1080 --fps 12
```

按输出总帧数抽帧：

```powershell
uv run designtoolkit video-to-frames .\demo.mp4 --frames 120 --overwrite
```

参数说明：

- `video_path`：输入视频文件路径，支持 FFmpeg 可解码的常见格式，例如 `mp4`、`mov`、`m4v`、`avi`、`mkv`、`webm`
- `--output-dir PATH`：序列帧保存目录，默认是 `{视频名}_frames`
- `--format {png,jpg,jpeg}`：输出图片格式，默认 `png`
- `--size WIDTHxHEIGHT`：输出尺寸，例如 `1920x1080`
- `--fps FLOAT`：输出帧率
- `--frames INT`：输出总帧数，会根据视频时长均匀抽帧
- `--overwrite`：允许写入已存在的非空输出目录

`--fps` 和 `--frames` 不能同时使用。

### 修改视频或图片尺寸

默认导出到源文件同级目录：

```powershell
uv run designtoolkit resize-media .\demo.mp4 800x600
uv run designtoolkit resize-media .\photo.png 800x600
```

默认行为：

- 输出路径：源文件同级目录下的 `{原文件名}_resized.{原扩展名}`
- 输出尺寸：保持原始宽高比，并放入指定最大边界内
- 覆盖策略：默认不覆盖已有输出文件

指定输出路径：

```powershell
uv run designtoolkit resize-media .\demo.mp4 1280x720 .\output.mp4
uv run designtoolkit resize-media .\photo.jpg 800x600 .\small.jpg
```

参数说明：

- `input_path`：输入视频或图片文件路径，支持 FFmpeg 可解码的主流格式
- `size`：最大输出尺寸，例如 `800x600`
- `output_path`：可选，生成文件保存路径

`800x600` 表示输出宽度不超过 `800` 且输出高度不超过 `600`。工具不会强制拉伸图片或视频到固定宽高。

## Web GUI

启动 Web 图形界面：

```powershell
uv run designtoolkit web
```

默认访问地址：http://127.0.0.1:8765

支持功能：

- 视频转 PNG/JPG/JPEG 序列帧
- 路径字段支持通过文件夹按钮唤起本机文件/目录选择器，并会记住上一次选择目录；不可用时会回退到内联编辑器手动填写本地路径
- 视频转帧支持在输入路径中按行填写多个视频，任务会按顺序逐个处理；填写输出目录时会作为批量输出根目录使用
- 视频或图片尺寸修改；输入路径支持按行填写多个媒体文件，批量任务会逐个处理并自动生成 `_resized` 输出路径
- 实时任务状态追踪
- 任务执行日志实时查看（SSE 推送），包含单个素材和任务总耗时

### Studio Chroma Keyer

在以上命令启动的本地 Web GUI 中，点击侧边栏的 `Studio Chroma Keyer`：

- 接受一张本地 PNG、JPG、JPEG 图片，或一个 MOV、MP4、M4V、WebM、MKV、AVI
  视频；视频是否可读取取决于随应用分发的 FFmpeg 解码能力
- 图片或视频路径经选择、拖放解析、Enter 或失焦确认后自动加载；视频显示当前帧
  预览、时间轴、帧率、源尺寸和估算帧数，连续 seek 只保留最新结果
- 在当前图片或视频帧上吸取实际背景色；仅支持一个 RGB 通道明确主导的幕色，
  通常为绿色、蓝色或红色；青色、黄色、品红色和中性色会被拒绝
- `Screen Color` 以及 Screen Gain、Screen Balance、Clip Black、Clip White、
  Shrink/Grow、Feather、Despill Amount 七个参数在整个视频中保持不变
- 图片和当前视频帧均支持 `Final` 与 `Alpha` 预览；图片导出原图尺寸 RGBA PNG
- 视频按完整时长和原始解码帧率导出源尺寸 RGBA PNG 序列，帧名为
  `<视频名>_keyed_%06d.png`，从 1 开始连续编号
- 视频输出目录包含 `manifest.json`，记录源路径、尺寸、帧率、时长、实际帧数、
  命名模式、Screen Color 和全部七项参数
- 视频导出显示逐帧进度；内容先写入临时目录，全部完成后原子发布
  `<视频名>_keyed_frames`。图片文件和视频目录均不覆盖已有输出
- v1 不处理音频，不提供取消、入点/出点范围、批量抠像、参数关键帧或图片序列输入

前端开发（热重载模式，端口 5173）：

```powershell
cd web/frontend
npm install
npm run dev
```

## 验证

```powershell
uv run pytest
uv run ruff check .
uv run python -m compileall src tests
```

## 仓库结构

```text
.
├── docs/       # 项目文档和计划
├── scripts/    # 本地自动化脚本
├── src/        # 应用源码
│   └── designtoolkit/
└── tests/      # 自动化测试
```
