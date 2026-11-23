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

## Web GUI

启动 Web 图形界面：

```powershell
uv run designtoolkit web
```

默认访问地址：http://127.0.0.1:8765

支持功能：

- 视频转 PNG/JPG/JPEG 序列帧
- 图片／视频色键抠图，支持 `Final` / `Alpha` 预览和透明素材导出
- 保留 MOV 原生 Alpha，导出左侧黑白 Alpha、右侧 RGB 的拼接 MP4，以及同名 JSON 和配套透明播放器
- 路径字段支持通过文件夹按钮唤起本机文件/目录选择器，并会记住上一次选择目录；不可用时会回退到内联编辑器手动填写本地路径
- 本地图片和视频可直接拖入输入框。浏览器未提供真实路径时，按实际文件内容导入到
  `.temp/local-imports/` 并自动加载；原文件不变，多文件输入支持合并拖入。
  此类副本的默认输出位于导入目录，可通过输出路径/目录字段指定保存位置。
- 视频转帧支持在输入路径中按行填写多个视频，任务会按顺序逐个处理；填写输出目录时会作为批量输出根目录使用
- 视频或图片尺寸修改；输入路径支持按行填写多个媒体文件，批量任务会逐个处理并自动生成 `_resized` 输出路径
- 实时任务状态追踪
- 任务执行日志实时查看（SSE 推送），包含单个素材和任务总耗时


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


### 色键抠图

在以上命令启动的本地 Web GUI 中，点击侧边栏的 `色键抠图`：

- 接受一张本地 PNG、JPG、JPEG 图片，或一个 MOV、MP4、M4V、WebM、MKV、AVI
  视频；视频是否可读取取决于随应用分发的 FFmpeg 解码能力
- 图片或视频路径经选择、拖放解析、Enter 或失焦确认后自动加载；视频显示当前帧
  预览、时间轴、帧率、源尺寸和估算帧数，连续 seek 只保留最新结果
- 视频时间轴提供播放/暂停、回到开头、上一帧和下一帧控制，逐帧操作按源帧率
  步进并在首帧和末帧处停止
- 色键模式未采样时播放原始视频帧；采样后播放当前 `Final` 或 `Alpha` 抠像预览。
  原始透明通道模式无需采样，直接播放源 Alpha 对应的 `Final` / `Alpha` 预览。
- 播放按真实时间推进且最多保留一个预览请求在途；解码慢于源帧率时丢弃中间
  预览帧，下一次请求追赶到最新帧时间
- 当前预览播放不支持音频、循环、速度控制或入点/出点范围播放
- 在当前图片或视频帧上吸取实际背景色；仅支持一个 RGB 通道明确主导的幕色，
  通常为绿色、蓝色或红色；青色、黄色、品红色和中性色会被拒绝
- `Screen Color` 以及 Screen Gain、Screen Balance、Clip Black、Clip White、
  Shrink/Grow、Feather、Despill Amount 七个参数在整个视频中保持不变
- 图片和当前视频帧均支持 `Final` 与 `Alpha` 预览；图片导出原图尺寸 RGBA PNG
- PNG 序列模式按完整时长和原始解码帧率导出源尺寸 RGBA PNG 序列，帧名为
  `<视频名>_keyed_%06d.png`，从 1 开始连续编号
- PNG 序列输出目录包含 `manifest.json`，记录源路径、尺寸、帧率、时长、实际帧数、
  命名模式、Screen Color 和全部七项参数
- 视频新增默认输出格式 `MP4（颜色 + 透明遮罩）`，仍可选择 `PNG 序列`。
  MP4 左侧保存黑白灰度 Alpha，右侧保存非预乘 RGB；这是自定义布局，普通播放器
  显示左右拼接画面，必须用配套播放器恢复透明效果。
  JSON 布局为 `alpha_left_color_right`；配套播放器仍兼容旧的 `color_left_alpha_right` 文件。
- MP4 使用 H.264/yuv420p，并默认保留第一条音轨为 AAC；无音轨也可导出。
  首版要求恒定帧率，变帧率素材会提示先转换；字幕、多音轨不保留。
- 本地 MP4 默认生成 `<视频名>_keyed_packed.mp4`、同名 JSON 和配套
  `<输出名>_player` 目录。奇数尺寸通过补齐编码、按 JSON 裁切显示；
  Alpha 补齐为零，RGB 补齐延续边缘颜色，以减少原始边缘像素的压缩暗边。
  压缩有损，包括透明遮罩。
- 托管模式下载包包含一个 MP4、JSON 和播放器。解压后直接用浏览器打开
  播放器的 `index.html`，选择 MP4 和 JSON，再点击播放；播放器支持
  棋盘格、浅色、深色背景检查透明边缘，无需 CDN 或额外服务。
- 播放器接入 API 及 CORS 要求见
  [`player/README.md`](src/designtoolkit/chroma_keyer/player/README.md)。
  旧 API 请求未指定 `output_format` 时继续导出 PNG；新格式使用
  `output_format: "packed_alpha_mp4"`，本地可选 `output_path`。
- PNG 序列导出显示逐帧进度；内容先写入临时目录，全部完成后原子发布
  `<视频名>_keyed_frames`。图片文件和视频目录均不覆盖已有输出
- v1 不提供取消、批量抠像、参数关键帧或图片序列输入

### 保留 MOV 自带透明信息

在 Web GUI 中按以下步骤导出：

1. 打开 `色键抠图`，选择或拖入带原生 Alpha 的 MOV。
2. 将 `透明来源` 设为 `视频原始透明通道`，无需吸取背景色。
3. 在 `Alpha 解释` 中选择 `直通 Alpha` 或 `预乘 Alpha`，用 `Final` / `Alpha` 检查结果。
4. 点击 `导出 MP4`；打开结果目录中的 `<输出名>_player/index.html`，
   选择输出 MP4 和同名 JSON，恢复透明显示。

此模式固定导出 `MP4（颜色 + 透明遮罩）`：**左侧为黑白灰度 Alpha，右侧为 RGB 内容**，
JSON 的 `layout` 为 `alpha_left_color_right`。不应用色键、羽化或去杂色参数。

颜色解释默认使用 `直通`；若素材的 RGB 已乘以 Alpha，请选择 `预乘`，
导出时会还原 RGB。应用不会自动猜测素材的颜色解释。
支持 FFmpeg 可解码 Alpha 的 MOV，包括 QTRLE 和 ProRes 4444；
普通 H.264 MOV 没有原生 Alpha，选择此模式会明确报错。

结果包含 MP4、同名 JSON 和离线播放器，第一条音轨保留为 AAC。
普通播放器显示左右拼接画面；配套播放器按 JSON 恢复透明显示，
同时兼容旧的 `color_left_alpha_right` 文件。调整导出布局后，请重新导出新文件。
H.264 压缩对颜色和透明遮罩均有损，高位深 Alpha 会转换为 8 位；
本模式保持现有恒定帧率要求。
API 示例：`POST /api/tasks/chroma-keyer-video`，可省略 `samples` 和 `params`。

```json
{
  "input_path": "E:\\media\\native.mov",
  "alpha_source": "source",
  "source_alpha_mode": "straight",
  "output_format": "packed_alpha_mp4",
  "output_path": "E:\\exports\\native_packed.mp4"
}
```

`source_alpha_mode` 可选 `"straight"`（默认）或 `"premultiplied"`。
视频预览接口 `POST /api/chroma-keyer/videos/preview` 使用同样的来源字段，
另传 `timestamp_seconds` 和 `view_mode: "final" | "alpha"`。
使用步骤与真实素材验收见
[`docs/source-alpha-mov-acceptance.md`](docs/source-alpha-mov-acceptance.md)。

前端开发（热重载模式，端口 5173）：

```powershell
cd web/frontend
npm install
npm run dev
```

## 公网部署

默认 Compose 配置面向至少 8 vCPU、16 GiB 内存和 100 GB SSD 的单机部署。
部署前，将域名的 A/AAAA 记录指向服务器，并确保公网可访问 80 和 443 端口。
Caddy 会根据 `.env` 中的 `DOMAIN` 自动申请和续期 HTTPS 证书。
部署需要 Docker Compose v2.6.0 或更高版本。

首次部署（Linux VPS，Bash）：

```bash
cp .env.example .env
docker compose up -d --build
```

首次部署（Windows PowerShell）：

```powershell
Copy-Item .env.example .env
docker compose up -d --build
```

部署前应检查 `.env` 中的域名、安全限制和各服务资源限制。默认配置允许两个
Worker 各预留最多 5 GiB 输出空间，并始终保留 5 GiB 基础余量，因此两个输出
任务开始前至少需要 15 GiB 可用磁盘空间。

公网安全硬边界采用默认值作为最大允许值：单次上传 500 MiB、会话 2 GiB、输出
5 GiB、排队任务 20、上传并发 4、预览并发 2、图片 100M 像素、视频
3840x2160/1800 秒。对应 `.env` 最大值变量只能收紧；`MIN_FREE_BYTES` 至少为
5 GiB，只能提高。越界配置会被钳制并记录 warning。

托管超时与租约设置：

- `DESIGNTOOLKIT_TASK_TIMEOUT_SECONDS` 默认 7200 秒，仅控制 RQ 任务执行上限；
  artifact 保留时间由独立的 `DESIGNTOOLKIT_RETENTION_SECONDS` 控制。
- `DESIGNTOOLKIT_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS` 和
  `DESIGNTOOLKIT_REDIS_SOCKET_TIMEOUT_SECONDS` 默认均为 5 秒。
- `DESIGNTOOLKIT_RESERVATION_TTL_SECONDS` 与
  `DESIGNTOOLKIT_RESERVATION_HEARTBEAT_SECONDS` 默认分别为 60 秒和 5 秒。
- `DESIGNTOOLKIT_LEASE_TTL_SECONDS` 与
  `DESIGNTOOLKIT_LEASE_HEARTBEAT_SECONDS` 默认分别为 60 秒和 5 秒。

Caddy 对请求体保留 525 MB 上限，并将完整请求体读取时间限制为 30 分钟。

观察服务状态和应用日志：

```powershell
docker compose ps
docker compose logs -f app worker-1 worker-2
```

升级镜像和应用：

```bash
docker compose pull redis caddy
docker compose up -d --build
```

`redis` 和 `caddy` 使用远端镜像；`app` 在本地构建
`designtoolkit:latest`，`worker-1`、`worker-2` 和 `cleanup` 复用该本地镜像。

停止服务：

```powershell
docker compose down
```

`app_data` 保存上传和输出，`redis_data` 保存队列及任务状态，`caddy_data` 和
`caddy_config` 保存 Caddy 状态。`docker compose down` 默认保留这些命名
volume；升级或迁移前应备份它们。不要使用 `docker compose down -v`，除非已
确认可以永久删除持久数据。

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
|       ├── chroma_keyer/ # 色键抠图工具
|       ├── resize_media/ # 视频或图片尺寸修改工具
|       └── video_to_frames/ # 视频转序列帧工具
├── web/        # Web GUI 源码
│   └── frontend/ # 前端源码
└── tests/      # 自动化测试
```
