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

## 视频转序列帧

默认导出：

```powershell
uv run designtoolkit video-to-frames .\demo.mp4
```

默认行为：

- 输出目录：视频同级目录下的 `demo_frames`
- 输出格式：`png`
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

- `video_path`：输入视频文件路径，支持 FFmpeg 可解码的常见格式，例如 `mp4`、`mov`、`m4v`、`avi`、`mkv`
- `--output-dir PATH`：序列帧保存目录，默认是 `{视频名}_frames`
- `--format {png,jpg,jpeg}`：输出图片格式，默认 `png`
- `--size WIDTHxHEIGHT`：输出尺寸，例如 `1920x1080`
- `--fps FLOAT`：输出帧率
- `--frames INT`：输出总帧数，会根据视频时长均匀抽帧
- `--overwrite`：允许写入已存在的非空输出目录

`--fps` 和 `--frames` 不能同时使用。

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
