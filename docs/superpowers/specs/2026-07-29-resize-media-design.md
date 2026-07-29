# Resize Media CLI 设计规格

## 目标

新增一个命令行工具，用于修改视频或图片尺寸。输出结果必须保持源文件宽高比，并放入用户指定的最大尺寸边界内。

## 已确认需求

- 命令接收一个输入媒体路径、一个尺寸参数，以及一个可选输出路径。
- 尺寸参数格式为 `WIDTHxHEIGHT`，例如 `800x600`。
- 输出保持原始宽高比，不强制拉伸到固定宽高。
- `800x600` 表示输出宽度不超过 `800`，输出高度不超过 `600`。
- 不传输出路径时，默认输出到源文件同级目录。
- 默认输出文件名为 `{原文件名}_resized.{原扩展名}`。
- 输入是视频时，输出修改尺寸后的视频。
- 输入是图片时，输出修改尺寸后的图片。
- 通过 FFmpeg 支持主流视频和图片格式。

## 命令接口

```powershell
uv run designtoolkit resize-media <input_path> <size> [output_path]
```

示例：

```powershell
uv run designtoolkit resize-media .\demo.mp4 800x600
uv run designtoolkit resize-media .\demo.png 800x600 .\out.png
```

参数说明：

- `input_path`：输入视频或图片文件路径。
- `size`：最大输出尺寸，格式为 `WIDTHxHEIGHT`。
- `output_path`：可选，生成文件保存路径。

## 范围

本次包含：

- 在现有 `designtoolkit` CLI 中新增 `resize-media` 子命令。
- 复用现有 `video-to-frames` 使用的 bundled FFmpeg 后端。
- 校验输入不存在、输入不是文件、尺寸格式非法、输出路径已存在等场景。
- 默认安全策略：不覆盖已有输出文件。
- 视频处理时尽量保留原音频流。
- 增加聚焦的 pytest 覆盖：CLI 参数传递、默认输出命名、FFmpeg 命令构造、保持比例缩放、错误处理。
- 更新 `README.md` 使用说明。

本次不包含：

- 原地覆盖源文件。
- `--overwrite` 参数。
- 裁剪、补边、旋转、帧率调整、编码器选择、质量参数、元数据编辑。
- 引入 Pillow、OpenCV 等独立图片处理依赖。

## 架构

新增独立模块：

```text
src/designtoolkit/media_resize/
|-- __init__.py
`-- core.py
```

`core.py` 负责尺寸修改能力的领域模型、参数校验、默认输出路径、FFmpeg 命令构造，以及公开的 resize 函数。

现有 `designtoolkit.video_frames.ffmpeg_backend.FfmpegBackend` 已经提供 bundled FFmpeg 路径获取和 `run()` 方法，可以直接复用，避免新增后端实现。

`src/designtoolkit/main.py` 新增 `resize-media` 子命令，并沿用当前 `video-to-frames` 的测试注入方式：主流程支持传入 resize 函数和 backend factory，测试中用 fake backend 验证参数和错误路径。

## 数据流

1. CLI 解析 `<input_path>`、`<size>`、可选 `[output_path]`。
2. CLI 使用现有 `parse_size()` 行为解析尺寸。
3. CLI 构造 `MediaResizeOptions`。
4. core 层校验：
   - 输入路径存在；
   - 输入路径是文件；
   - 输出路径不等于输入路径；
   - 输出路径不存在。
5. core 层从 backend 获取 bundled FFmpeg 可执行文件。
6. core 层构造保持比例的 FFmpeg scale filter：

```text
scale=WIDTH:HEIGHT:force_original_aspect_ratio=decrease
```

7. backend 执行 FFmpeg 命令。
8. core 层把 FFmpeg 失败映射为用户可读错误，退出码为 `1`。
9. CLI 打印生成文件路径并返回退出码 `0`。

## 输出路径规则

如果用户传入 `output_path`，直接使用该路径。

如果用户不传 `output_path`，按以下规则生成：

```python
input_path.parent / f"{input_path.stem}_resized{input_path.suffix}"
```

示例：

- `demo.mp4` -> `demo_resized.mp4`
- `photo.jpg` -> `photo_resized.jpg`
- `C:\work\banner.png` -> `C:\work\banner_resized.png`

如果显式或默认输出路径已经存在，命令失败并提示错误，不覆盖已有文件。

## 格式支持

工具依赖 bundled FFmpeg 的解码和编码能力，不单独维护严格扩展名白名单。

预期支持的主流格式包括：

- 视频：`mp4`、`mov`、`m4v`、`avi`、`mkv`、`webm`
- 图片：`png`、`jpg`、`jpeg`、`webp`、`bmp`、`tif`、`tiff`

不支持或损坏的输入文件由 FFmpeg 失败输出转换成用户可读错误。

## 错误处理

新增 `MediaResizeError`，形态与现有 `VideoFrameError` 保持一致：

- 参数和路径校验错误退出码为 `2`；
- backend 不可用或 FFmpeg 处理失败退出码为 `1`；
- 错误打印到 stderr，格式为 `error: <message>`；
- 预期用户错误不显示 Python traceback。

错误信息需要明确说明失败原因，方便 CLI 用户直接修正输入。

## 测试策略

按 TDD 执行，先写失败测试，再实现功能。

新增 `tests/test_media_resize.py` 覆盖：

- 复用 `parse_size` 解析合法尺寸；
- 计算默认输出路径；
- 构造包含 `scale=WIDTH:HEIGHT:force_original_aspect_ratio=decrease` 的 FFmpeg 命令；
- 缺失输入路径报错；
- 输入路径不是文件时报错；
- 输出路径已存在时报错；
- 输出路径等于输入路径时报错；
- backend 不可用时在产生输出副作用前报错；
- FFmpeg 非零退出码映射为退出码 `1`。

扩展 `tests/test_main.py` 覆盖：

- `resize-media --help` 不创建 backend；
- CLI 把完整 resize options 传给 resize 函数；
- CLI 默认使用 `FfmpegBackend`；
- CLI 正确报告 `MediaResizeError` 的退出码和 stderr；
- 尺寸格式错误时，在执行 resize 前失败。

最终验证命令：

```powershell
uv run pytest
uv run ruff check .
uv run python -m compileall src tests
```

## 文档

更新 `README.md`，补充：

- 命令示例；
- 参数说明；
- 默认输出命名；
- 保持宽高比的行为；
- 默认不覆盖已有输出文件；
- 如验证命令有变化，同步更新验证说明。

## 待决事项

无。用户已确认：

- 默认输出命名为 `{原文件名}_resized.{原扩展名}`；
- `WIDTHxHEIGHT` 是最大边界，不是强制固定输出宽高；
- 使用现有 FFmpeg 方案实现视频和图片尺寸修改。
