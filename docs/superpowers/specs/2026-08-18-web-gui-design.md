# DesignToolkit Web GUI Design

## 背景

DesignToolkit 当前是一个 Python CLI 工具，已有两个核心能力：

- `video-to-frames`：将视频导出为 PNG/JPG/JPEG 序列帧，WebM 导出 PNG 时保留 alpha 通道。
- `resize-media`：按最大宽高调整图片或视频尺寸，并保持宽高比。

第一版 Web GUI 的目标是在浏览器中提供简约、易操作、可持续扩展的本地工具台，同时继续复用现有 Python 媒体处理逻辑，避免前端重写 FFmpeg 行为。

## 目标

- 提供本地 Web GUI，启动后可在浏览器中操作。
- 第一版支持“视频转 PNG 序列”和“媒体尺寸修改”两个模块。
- 提供统一日志面板，实时显示任务参数、执行状态、成功结果和错误信息。
- 保持界面简约、美观、偏工具型，打开后直接进入可操作界面。
- 采用模块化架构，为后续新增批处理、格式转换、透明通道检测等功能预留扩展边界。
- 保持现有 CLI 行为和测试不回归。

## 非目标

- 第一版不做用户系统、权限系统或远程部署。
- 第一版不做浏览器文件上传、文件下载打包或远程文件管理。
- 第一版不引入数据库；任务历史只保留在当前服务进程内。
- 第一版不解析 FFmpeg 百分比进度；日志只提供实时阶段信息和最终状态。
- 第一版不支持多个 FFmpeg 任务并发执行。

## 技术路线

采用 FastAPI + React/Vite + TypeScript：

- 后端使用 FastAPI 暴露本地 API，复用 `designtoolkit.video_frames` 和 `designtoolkit.media_resize`。
- 前端使用 React/Vite/TypeScript 构建工具型界面。
- 前端状态管理使用 Zustand，表单状态保留在模块组件内，任务和日志状态全局共享。
- 日志通过 Server-Sent Events 推送到前端。
- 第一版服务默认绑定 `127.0.0.1`，定位为本地工具台。

## 运行入口

新增 CLI 子命令：

```powershell
uv run designtoolkit web
```

默认启动地址：

```text
http://127.0.0.1:8765
```

后端在开发模式下可配合 Vite dev server；生产构建后由 FastAPI 提供静态前端资源。

## 后端结构

建议新增模块：

```text
src/designtoolkit/web/
  __init__.py
  app.py
  routes.py
  schemas.py
  tasks.py
  logs.py
```

职责边界：

- `app.py`：创建 FastAPI app，挂载 API 路由和静态资源。
- `routes.py`：定义任务创建、任务查询和日志事件接口。
- `schemas.py`：定义请求、响应、错误和日志事件模型。
- `tasks.py`：管理内存任务、状态流转和后台执行。
- `logs.py`：定义日志事件结构和订阅机制。

## API 设计

```text
GET  /api/health
POST /api/tasks/video-frames
POST /api/tasks/media-resize
GET  /api/tasks/{task_id}
GET  /api/tasks/{task_id}/events
```

### 视频转序列帧任务

请求体：

```json
{
  "input_path": "E:\\path\\input.webm",
  "output_dir": "E:\\path\\output_frames",
  "image_format": "png",
  "size": "250x250",
  "fps": null,
  "frames": null,
  "overwrite": true
}
```

规则：

- `input_path` 必填。
- `output_dir` 可选；未填写时沿用 CLI 默认输出目录规则。
- `image_format` 默认为 `png`，支持 `png`、`jpg`、`jpeg`。
- `size` 可选，格式为 `WIDTHxHEIGHT`。
- `fps` 和 `frames` 互斥。
- `overwrite` 默认为 `false`。
- WebM 导出 PNG 时沿用现有后端逻辑保留透明通道。

### 媒体尺寸修改任务

请求体：

```json
{
  "input_path": "E:\\path\\input.png",
  "output_path": "E:\\path\\output.png",
  "size": "250x250"
}
```

规则：

- `input_path` 和 `size` 必填。
- `output_path` 可选；未填写时沿用 CLI 默认输出路径规则。
- `size` 格式为 `WIDTHxHEIGHT`。
- 输出路径不能等于输入路径。
- 输入图片输出图片，输入视频输出视频，沿用现有跨媒体类型校验。
- 输出文件已存在时沿用现有 CLI 策略：报错，不覆盖。

### 任务创建响应

```json
{
  "task_id": "uuid",
  "status": "queued"
}
```

### 任务查询响应

```json
{
  "task_id": "uuid",
  "kind": "video_frames",
  "status": "running",
  "created_at": "2026-08-18T10:00:00",
  "started_at": "2026-08-18T10:00:01",
  "finished_at": null,
  "input_path": "E:\\path\\input.webm",
  "output_path": "E:\\path\\output_frames",
  "error": null
}
```

## 任务模型

任务状态：

```text
queued -> running -> succeeded
queued -> running -> failed
```

第一版采用内存任务管理：

- FastAPI 接收请求后创建 `task_id`。
- 后端将请求转换为现有 `VideoFrameOptions` 或 `MediaResizeOptions`。
- 媒体处理在后台线程中执行，避免阻塞 HTTP 请求。
- 同一时间只允许一个任务运行。
- 若已有任务处于 `queued` 或 `running`，新的任务请求返回用户可读错误。
- 服务重启后任务历史清空。

## 日志事件设计

日志通过 SSE 推送：

```text
GET /api/tasks/{task_id}/events
```

事件结构：

```json
{
  "task_id": "uuid",
  "timestamp": "2026-08-18T10:00:02",
  "level": "info",
  "phase": "validating",
  "message": "Input file exists: E:\\path\\input.webm",
  "details": {}
}
```

日志等级：

- `info`
- `progress`
- `success`
- `error`

日志阶段：

- `created`
- `validating`
- `preparing`
- `running`
- `completed`
- `failed`

第一版日志内容：

- 任务创建。
- 参数摘要。
- 输入路径和输出路径校验。
- FFmpeg 执行开始。
- 成功输出路径。
- 失败错误摘要。

日志面板需要支持：

- 实时追加。
- 自动滚动到底部。
- 清空日志。
- 复制全部日志文本。

## 错误处理

API 错误统一返回用户可读结构：

```json
{
  "error": {
    "code": "output_exists",
    "message": "Output path already exists: E:\\path\\output.png"
  }
}
```

错误来源：

- 参数错误：路径为空、尺寸格式错误、`fps` 和 `frames` 同时设置。
- 业务错误：输出目录非空、输出路径已存在、输入输出媒体类型不一致。
- 执行错误：FFmpeg 不可用、FFmpeg 返回非 0、无法读取视频时长。

## 前端结构

```text
web/frontend/src/
  app/
    App.tsx
    AppShell.tsx
    routes.ts
    types.ts

  modules/
    video-frames/
      VideoFramesPage.tsx
      VideoFramesForm.tsx
      videoFramesDefaults.ts
      videoFramesValidation.ts

    media-resize/
      MediaResizePage.tsx
      MediaResizeForm.tsx
      mediaResizeDefaults.ts
      mediaResizeValidation.ts

  components/
    Sidebar.tsx
    HeaderBar.tsx
    LogPanel.tsx
    TaskStatusBadge.tsx
    PathInput.tsx
    SizeInput.tsx
    SubmitButton.tsx
    FieldError.tsx

  services/
    apiClient.ts
    taskEvents.ts
    taskTypes.ts

  state/
    useTaskStore.ts
    useLogStore.ts
```

## 前端布局

第一版采用三段式工具台：

```text
HeaderBar
Sidebar + ActiveModulePage
LogPanel
```

布局规则：

- 顶部展示产品名、当前任务状态和基础入口。
- 左侧导航固定宽度，只放功能模块。
- 中间区域显示当前模块表单和执行按钮。
- 底部日志面板全局共享，不随模块切换清空。
- 不设计首页或营销页，打开后直接进入可操作界面。

## 前端状态管理

使用 Zustand 管理跨模块状态：

- `useTaskStore`：当前任务 ID、任务类型、状态、结果路径、错误信息、开始时间、结束时间。
- `useLogStore`：日志事件列表、自动滚动状态、清空日志、复制日志文本。

表单状态保留在各自模块组件内：

- `VideoFramesForm` 管理视频转序列帧参数。
- `MediaResizeForm` 管理媒体尺寸修改参数。

任务状态：

```text
idle | queued | running | succeeded | failed
```

表单锁定规则：

- `queued` 和 `running` 时锁定当前表单。
- `succeeded` 和 `failed` 后恢复表单。
- 第一版同一时间只允许一个任务运行。

## 模块扩展方式

前端通过模块配置表注册功能：

```ts
type ToolModule = {
  id: "video_frames" | "media_resize"
  label: string
  route: string
  component: React.ComponentType
}
```

后续新增功能时新增模块目录并注册到模块列表，避免修改主布局核心逻辑。

## 测试策略

后端测试使用 `pytest`：

- API 测试：`GET /api/health`、两个任务创建接口、任务状态查询、错误响应格式。
- 任务模型测试：状态流转、单任务运行限制、任务完成后结果路径记录。
- 日志事件测试：任务创建、参数校验、执行开始、成功、失败均产生结构化日志。
- 适配层测试：Web 请求能正确转换为现有 `VideoFrameOptions` 和 `MediaResizeOptions`。
- 回归测试：保留现有 CLI 测试，确保 Web GUI 不破坏 CLI 行为。

前端测试使用 Vitest + React Testing Library：

- 表单校验：尺寸格式、必填路径、`fps` 和 `frames` 互斥。
- 交互测试：提交后表单锁定，任务结束后恢复。
- 日志面板：接收事件后追加日志，支持清空和复制。
- 状态管理：`useTaskStore` 和 `useLogStore` 的状态流转。
- 模块导航：切换模块不清空全局日志。

端到端测试使用 Playwright smoke test：

- 页面能打开。
- 两个模块能切换。
- 表单能填写。
- 任务提交流程能触发 API mock 或测试后端。

真实媒体处理继续由 Python 后端测试覆盖；可保留 `assets/video/test.webm` 用于手动验收透明 PNG 导出链路。

## 验证命令

后端：

```powershell
uv run pytest
uv run ruff check .
uv run python -m compileall src tests
```

前端：

```powershell
cd web/frontend
npm run test
npm run lint
npm run build
```

端到端：

```powershell
cd web/frontend
npm run test:e2e
```

## 验收标准

- `uv run designtoolkit web` 能启动本地服务。
- 浏览器打开本地地址后直接进入工具台界面。
- 左侧可以在 `视频转 PNG 序列` 和 `媒体尺寸修改` 两个模块间切换。
- 视频转 PNG 序列模块能提交任务，并调用现有 `video-to-frames` 逻辑。
- WebM 转 PNG 时透明通道保留，不产生回归。
- 媒体尺寸修改模块能提交任务，并调用现有 `resize-media` 逻辑。
- 日志面板能实时显示任务开始、参数摘要、执行中、成功或失败。
- 任务运行时表单锁定，任务结束后恢复。
- 参数错误和执行错误都以用户可读方式显示。
- 现有 CLI 行为和测试不被破坏。
- 后端 `pytest`、`ruff`、`compileall` 通过。
- 前端测试、lint、build 通过。
- README 增加 Web GUI 启动方式和第一版功能说明。

