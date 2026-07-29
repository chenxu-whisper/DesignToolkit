# Resize Media CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `resize-media` 命令，通过 FFmpeg 将视频或图片按最大边界缩放，并保持原始宽高比。

**Architecture:** 新增 `designtoolkit.media_resize` 核心模块，复用现有 `FfmpegBackend` 的 bundled FFmpeg 查找和执行能力。CLI 在 `designtoolkit.main` 中增加子命令，并沿用当前依赖注入测试模式。

**Tech Stack:** Python 3.12, argparse, pathlib, dataclasses, pytest, Ruff, imageio-ffmpeg bundled FFmpeg.

---

## 前置检查

当前工作区存在未提交改动：

```text
M tests/test_main.py
```

执行实现前必须先确认该改动属于当前 resize-media 工作，或者由用户处理。不能回退、覆盖或静默丢弃这部分改动。若在当前工作区执行，修改 `tests/test_main.py` 前先读取文件并保留已有内容。

推荐在独立 worktree 中执行本计划，避免混入现有未提交改动。

## 文件结构

- Create: `src/designtoolkit/media_resize/__init__.py`
  - 导出 resize-media 核心 API，供 CLI 和测试使用。
- Create: `src/designtoolkit/media_resize/core.py`
  - 定义 `MediaResizeOptions`、`MediaResizeResult`、`MediaResizeError`、`ResizeBackend`、`default_output_path()`、`resize_media()`。
  - 构造 FFmpeg 命令并处理用户可读错误。
- Create: `tests/test_media_resize.py`
  - 覆盖核心模块行为，不调用真实 FFmpeg。
- Modify: `src/designtoolkit/main.py`
  - 新增 `resize-media` 子命令。
  - 扩展 `main()` 依赖注入参数，支持测试注入 resize 函数。
- Modify: `tests/test_main.py`
  - 增加 CLI 层测试；修改前先保留现有未提交内容。
- Modify: `README.md`
  - 增加命令用法、默认输出命名、保持比例和不覆盖说明。

## Task 1: 实现 media_resize 核心模块

**Files:**
- Create: `src/designtoolkit/media_resize/__init__.py`
- Create: `src/designtoolkit/media_resize/core.py`
- Create: `tests/test_media_resize.py`

- [ ] **Step 1: 写核心模块失败测试**

创建 `tests/test_media_resize.py`：

```python
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

import designtoolkit.media_resize as media_resize
from designtoolkit.media_resize import (
    MediaResizeError,
    MediaResizeOptions,
    ResizeBackendResult,
    default_output_path,
    resize_media,
)


class FakeResizeBackend:
    def __init__(
        self,
        run_result: ResizeBackendResult | None = None,
        ffmpeg_error: Exception | None = None,
    ) -> None:
        self.run_result = run_result or ResizeBackendResult(returncode=0)
        self.ffmpeg_error = ffmpeg_error
        self.commands: list[list[str]] = []

    def get_ffmpeg_exe(self) -> str:
        if self.ffmpeg_error is not None:
            raise self.ffmpeg_error
        return "ffmpeg"

    def run(self, command: Sequence[str]) -> ResizeBackendResult:
        self.commands.append(list(command))
        return self.run_result


def create_media(path: Path) -> Path:
    path.write_bytes(b"media")
    return path


def backend_unavailable_error_type() -> type[Exception]:
    error_type = getattr(media_resize, "BackendUnavailableError", None)
    assert error_type is not None, "BackendUnavailableError should be exported"
    assert issubclass(error_type, RuntimeError)
    return error_type


def test_default_output_path_uses_input_parent_stem_and_suffix(tmp_path: Path) -> None:
    input_path = tmp_path / "demo.mp4"

    assert default_output_path(input_path) == tmp_path / "demo_resized.mp4"


def test_resize_uses_default_output_and_aspect_ratio_scale_filter(
    tmp_path: Path,
) -> None:
    input_path = create_media(tmp_path / "demo.mp4")
    backend = FakeResizeBackend()

    result = resize_media(
        MediaResizeOptions(input_path=input_path, size=(800, 600)),
        backend,
    )

    output_path = tmp_path / "demo_resized.mp4"
    assert result.output_path == output_path
    assert result.command == tuple(backend.commands[0])
    assert backend.commands == [
        [
            "ffmpeg",
            "-hide_banner",
            "-n",
            "-i",
            str(input_path),
            "-vf",
            "scale=800:600:force_original_aspect_ratio=decrease",
            "-map",
            "0",
            "-c:a",
            "copy",
            str(output_path),
        ]
    ]


def test_resize_uses_explicit_output_path(tmp_path: Path) -> None:
    input_path = create_media(tmp_path / "photo.png")
    output_path = tmp_path / "small.png"
    backend = FakeResizeBackend()

    result = resize_media(
        MediaResizeOptions(
            input_path=input_path,
            size=(320, 240),
            output_path=output_path,
        ),
        backend,
    )

    assert result.output_path == output_path
    assert backend.commands[0][-1] == str(output_path)


def test_resize_rejects_missing_input(tmp_path: Path) -> None:
    backend = FakeResizeBackend()

    with pytest.raises(MediaResizeError, match="Input media does not exist") as exc:
        resize_media(
            MediaResizeOptions(input_path=tmp_path / "missing.mp4", size=(800, 600)),
            backend,
        )

    assert exc.value.exit_code == 2
    assert backend.commands == []


def test_resize_rejects_input_path_that_is_not_file(tmp_path: Path) -> None:
    backend = FakeResizeBackend()

    with pytest.raises(MediaResizeError, match="Input path is not a file") as exc:
        resize_media(MediaResizeOptions(input_path=tmp_path, size=(800, 600)), backend)

    assert exc.value.exit_code == 2
    assert backend.commands == []


def test_resize_rejects_existing_output_path(tmp_path: Path) -> None:
    input_path = create_media(tmp_path / "demo.mp4")
    output_path = create_media(tmp_path / "demo_resized.mp4")
    backend = FakeResizeBackend()

    with pytest.raises(MediaResizeError, match="Output path already exists") as exc:
        resize_media(
            MediaResizeOptions(
                input_path=input_path,
                size=(800, 600),
                output_path=output_path,
            ),
            backend,
        )

    assert exc.value.exit_code == 2
    assert backend.commands == []


def test_resize_rejects_output_path_equal_to_input_path(tmp_path: Path) -> None:
    input_path = create_media(tmp_path / "demo.mp4")
    backend = FakeResizeBackend()

    with pytest.raises(MediaResizeError, match="Output path must differ") as exc:
        resize_media(
            MediaResizeOptions(
                input_path=input_path,
                size=(800, 600),
                output_path=input_path,
            ),
            backend,
        )

    assert exc.value.exit_code == 2
    assert backend.commands == []


def test_resize_maps_backend_unavailable_before_running_ffmpeg(
    tmp_path: Path,
) -> None:
    input_path = create_media(tmp_path / "demo.mp4")
    backend = FakeResizeBackend(
        ffmpeg_error=backend_unavailable_error_type()("Bundled FFmpeg unavailable")
    )

    with pytest.raises(MediaResizeError, match="Bundled FFmpeg unavailable") as exc:
        resize_media(MediaResizeOptions(input_path=input_path, size=(800, 600)), backend)

    assert exc.value.exit_code == 1
    assert backend.commands == []


def test_resize_maps_ffmpeg_failure_to_exit_code_one_with_stderr(
    tmp_path: Path,
) -> None:
    input_path = create_media(tmp_path / "demo.mp4")
    backend = FakeResizeBackend(
        run_result=ResizeBackendResult(
            returncode=7,
            stderr="resize failed\n",
        )
    )

    with pytest.raises(MediaResizeError, match="resize failed") as exc:
        resize_media(MediaResizeOptions(input_path=input_path, size=(800, 600)), backend)

    assert exc.value.exit_code == 1
    assert "FFmpeg failed with exit code 7" in str(exc.value)
    assert "resize failed" in str(exc.value)
```

- [ ] **Step 2: 运行核心测试确认失败**

Run:

```powershell
uv run pytest tests/test_media_resize.py -v
```

Expected:

```text
ModuleNotFoundError: No module named 'designtoolkit.media_resize'
```

- [ ] **Step 3: 写最小核心实现**

创建 `src/designtoolkit/media_resize/core.py`：

```python
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from designtoolkit.video_frames import BackendUnavailableError


class MediaResizeError(Exception):
    """Expected user-facing error while resizing media."""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class ResizeBackendResult:
    returncode: int
    stderr: str = ""


@dataclass(frozen=True)
class MediaResizeResult:
    output_path: Path
    command: tuple[str, ...]


@dataclass(frozen=True)
class MediaResizeOptions:
    input_path: Path
    size: tuple[int, int]
    output_path: Path | None = None


class ResizeBackend(Protocol):
    def get_ffmpeg_exe(self) -> str:
        raise NotImplementedError

    def run(self, command: Sequence[str]) -> ResizeBackendResult:
        raise NotImplementedError


def default_output_path(input_path: Path) -> Path:
    return input_path.parent / f"{input_path.stem}_resized{input_path.suffix}"


def resize_media(
    options: MediaResizeOptions,
    backend: ResizeBackend,
) -> MediaResizeResult:
    input_path = options.input_path
    _validate_input_path(input_path)
    output_path = options.output_path or default_output_path(input_path)
    _validate_output_path(input_path=input_path, output_path=output_path)

    try:
        ffmpeg_exe = backend.get_ffmpeg_exe()
    except BackendUnavailableError as exc:
        raise MediaResizeError(str(exc), exit_code=1) from exc

    command = _build_ffmpeg_command(
        ffmpeg_exe=ffmpeg_exe,
        input_path=input_path,
        output_path=output_path,
        size=options.size,
    )
    result = backend.run(command)
    if result.returncode != 0:
        detail = f": {result.stderr.strip()}" if result.stderr.strip() else ""
        raise MediaResizeError(
            f"FFmpeg failed with exit code {result.returncode}{detail}",
            exit_code=1,
        )

    return MediaResizeResult(output_path=output_path, command=tuple(command))


def _validate_input_path(input_path: Path) -> None:
    if not input_path.exists():
        raise MediaResizeError(f"Input media does not exist: {input_path}")
    if not input_path.is_file():
        raise MediaResizeError(f"Input path is not a file: {input_path}")


def _validate_output_path(*, input_path: Path, output_path: Path) -> None:
    if output_path == input_path:
        raise MediaResizeError(
            f"Output path must differ from input path: {output_path}"
        )
    if output_path.exists():
        raise MediaResizeError(f"Output path already exists: {output_path}")


def _build_ffmpeg_command(
    *,
    ffmpeg_exe: str,
    input_path: Path,
    output_path: Path,
    size: tuple[int, int],
) -> list[str]:
    width, height = size
    return [
        ffmpeg_exe,
        "-hide_banner",
        "-n",
        "-i",
        str(input_path),
        "-vf",
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        "-map",
        "0",
        "-c:a",
        "copy",
        str(output_path),
    ]
```

创建 `src/designtoolkit/media_resize/__init__.py`：

```python
"""Media resize functionality."""

from designtoolkit.media_resize.core import (
    MediaResizeError,
    MediaResizeOptions,
    MediaResizeResult,
    ResizeBackend,
    ResizeBackendResult,
    default_output_path,
    resize_media,
)
from designtoolkit.video_frames import BackendUnavailableError

__all__ = [
    "BackendUnavailableError",
    "MediaResizeError",
    "MediaResizeOptions",
    "MediaResizeResult",
    "ResizeBackend",
    "ResizeBackendResult",
    "default_output_path",
    "resize_media",
]
```

- [ ] **Step 4: 运行核心测试确认通过**

Run:

```powershell
uv run pytest tests/test_media_resize.py -v
```

Expected:

```text
10 passed
```

- [ ] **Step 5: 提交核心模块**

Run:

```powershell
git add src/designtoolkit/media_resize tests/test_media_resize.py
git commit -m "feat: add media resize core"
```

Expected:

```text
Git creates a commit with message: feat: add media resize core
```

## Task 2: 接入 resize-media CLI

**Files:**
- Modify: `src/designtoolkit/main.py`
- Modify: `tests/test_main.py`

- [ ] **Step 1: 修改前读取当前 `tests/test_main.py`**

Run:

```powershell
Get-Content -Raw tests\test_main.py
git diff -- tests\test_main.py
```

Expected:

```text
Read succeeds. Any existing user changes are visible and preserved during edits.
```

- [ ] **Step 2: 写 CLI 失败测试**

在 `tests/test_main.py` 保留已有内容，并增加以下导入：

```python
from designtoolkit.media_resize import (
    MediaResizeError,
    MediaResizeOptions,
    MediaResizeResult,
)
```

把 `from designtoolkit.main import main` 保持不变。

在文件末尾追加测试：

```python
def test_resize_media_help_returns_zero_without_backend(capsys) -> None:
    def unavailable_resizer(
        options: MediaResizeOptions,
        backend: DummyBackend,
    ) -> MediaResizeResult:
        raise AssertionError("resizer should not be called for help")

    def unavailable_backend_factory() -> DummyBackend:
        raise AssertionError("backend should not be created for help")

    exit_code = main(
        ["resize-media", "--help"],
        media_resizer=unavailable_resizer,
        backend_factory=unavailable_backend_factory,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "input_path" in captured.out
    assert "size" in captured.out
    assert "output_path" in captured.out
    assert captured.err == ""


def test_resize_media_passes_complete_options_to_resizer(
    tmp_path: Path,
    capsys,
) -> None:
    input_path = tmp_path / "input.mp4"
    output_path = tmp_path / "output.mp4"
    backend = DummyBackend()
    calls: list[tuple[MediaResizeOptions, DummyBackend]] = []

    def resizer(
        options: MediaResizeOptions,
        received_backend: DummyBackend,
    ) -> MediaResizeResult:
        calls.append((options, received_backend))
        return MediaResizeResult(
            output_path=output_path,
            command=("ffmpeg",),
        )

    exit_code = main(
        [
            "resize-media",
            str(input_path),
            "800x600",
            str(output_path),
        ],
        media_resizer=resizer,
        backend_factory=lambda: backend,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == f"Resized media to {output_path}\n"
    assert captured.err == ""
    assert calls == [
        (
            MediaResizeOptions(
                input_path=input_path,
                size=(800, 600),
                output_path=output_path,
            ),
            backend,
        )
    ]


def test_resize_media_allows_default_output_path(
    tmp_path: Path,
    capsys,
) -> None:
    input_path = tmp_path / "input.png"
    backend = DummyBackend()
    observed_options: list[MediaResizeOptions] = []

    def resizer(
        options: MediaResizeOptions,
        received_backend: DummyBackend,
    ) -> MediaResizeResult:
        assert received_backend is backend
        observed_options.append(options)
        return MediaResizeResult(
            output_path=tmp_path / "input_resized.png",
            command=("ffmpeg",),
        )

    exit_code = main(
        ["resize-media", str(input_path), "320x240"],
        media_resizer=resizer,
        backend_factory=lambda: backend,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == f"Resized media to {tmp_path / 'input_resized.png'}\n"
    assert captured.err == ""
    assert observed_options == [
        MediaResizeOptions(
            input_path=input_path,
            size=(320, 240),
            output_path=None,
        )
    ]


def test_resize_media_uses_ffmpeg_backend_by_default(
    tmp_path: Path,
    capsys,
) -> None:
    input_path = tmp_path / "input.mp4"
    output_path = tmp_path / "input_resized.mp4"
    received_backends: list[FfmpegBackend] = []

    def fake_resizer(
        options: MediaResizeOptions,
        backend: FfmpegBackend,
    ) -> MediaResizeResult:
        assert options.input_path == input_path
        assert isinstance(backend, FfmpegBackend)
        received_backends.append(backend)
        return MediaResizeResult(
            output_path=output_path,
            command=("ffmpeg",),
        )

    exit_code = main(
        ["resize-media", str(input_path), "800x600"],
        media_resizer=fake_resizer,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == f"Resized media to {output_path}\n"
    assert captured.err == ""
    assert len(received_backends) == 1


def test_resize_media_returns_media_resize_error_exit_code(
    tmp_path: Path,
    capsys,
) -> None:
    input_path = tmp_path / "input.mp4"

    def resizer(
        options: MediaResizeOptions,
        backend: DummyBackend,
    ) -> MediaResizeResult:
        raise MediaResizeError("bad input", exit_code=2)

    exit_code = main(
        ["resize-media", str(input_path), "800x600"],
        media_resizer=resizer,
        backend_factory=DummyBackend,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err == "error: bad input\n"


def test_resize_media_rejects_bad_size_before_resizer(
    tmp_path: Path,
    capsys,
) -> None:
    input_path = tmp_path / "input.mp4"
    resizer_called = False

    def resizer(
        options: MediaResizeOptions,
        backend: DummyBackend,
    ) -> MediaResizeResult:
        nonlocal resizer_called
        resizer_called = True
        return MediaResizeResult(
            output_path=tmp_path / "input_resized.mp4",
            command=("ffmpeg",),
        )

    exit_code = main(
        ["resize-media", str(input_path), "bad"],
        media_resizer=resizer,
        backend_factory=DummyBackend,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert "Invalid size 'bad'" in captured.err
    assert not resizer_called
```

- [ ] **Step 3: 运行 CLI 新测试确认失败**

Run:

```powershell
uv run pytest tests/test_main.py -v
```

Expected:

```text
TypeError: main() got an unexpected keyword argument 'media_resizer'
```

- [ ] **Step 4: 修改 `src/designtoolkit/main.py` 接入子命令**

将 `src/designtoolkit/main.py` 更新为：

```python
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from designtoolkit.media_resize import (
    MediaResizeError,
    MediaResizeOptions,
    MediaResizeResult,
    ResizeBackend,
    resize_media,
)
from designtoolkit.video_frames import (
    ExportResult,
    FrameExportBackend,
    VideoFrameError,
    VideoFrameOptions,
    export_video_frames,
    parse_size,
)

Exporter = Callable[[VideoFrameOptions, FrameExportBackend], ExportResult]
MediaResizer = Callable[[MediaResizeOptions, ResizeBackend], MediaResizeResult]
BackendFactory = Callable[[], FrameExportBackend]


def main(
    argv: Sequence[str] | None = None,
    *,
    exporter: Exporter = export_video_frames,
    media_resizer: MediaResizer = resize_media,
    backend_factory: BackendFactory | None = None,
) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return _system_exit_code(exc)

    if args.command is None:
        print("DesignToolkit is ready.")
        return 0

    if args.command == "video-to-frames":
        try:
            backend = _create_backend(backend_factory)
            result = exporter(_video_frame_options(args), backend)
        except VideoFrameError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return exc.exit_code

        print(f"Exported frames to {result.output_dir}")
        return 0

    if args.command == "resize-media":
        try:
            backend = _create_backend(backend_factory)
            result = media_resizer(_media_resize_options(args), backend)
        except MediaResizeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return exc.exit_code

        print(f"Resized media to {result.output_path}")
        return 0

    raise AssertionError(f"Unhandled command: {args.command}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="designtoolkit")
    subparsers = parser.add_subparsers(dest="command")

    video_to_frames = subparsers.add_parser(
        "video-to-frames",
        help="Export a video to image frames.",
    )
    video_to_frames.add_argument("video_path", type=Path)
    video_to_frames.add_argument("--output-dir", type=Path)
    video_to_frames.add_argument(
        "--format",
        choices=("png", "jpg", "jpeg"),
        default="png",
        dest="image_format",
    )
    video_to_frames.add_argument("--size", type=_parse_size_argument)
    video_to_frames.add_argument("--fps", type=float)
    video_to_frames.add_argument("--frames", type=int)
    video_to_frames.add_argument("--overwrite", action="store_true")

    resize_media_parser = subparsers.add_parser(
        "resize-media",
        help="Resize a video or image while preserving aspect ratio.",
    )
    resize_media_parser.add_argument("input_path", type=Path)
    resize_media_parser.add_argument("size", type=_parse_size_argument)
    resize_media_parser.add_argument("output_path", nargs="?", type=Path)

    return parser


def _parse_size_argument(value: str) -> tuple[int, int]:
    try:
        return parse_size(value)
    except VideoFrameError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _create_backend(backend_factory: BackendFactory | None) -> FrameExportBackend:
    if backend_factory is not None:
        return backend_factory()

    from designtoolkit.video_frames.ffmpeg_backend import FfmpegBackend

    return FfmpegBackend()


def _video_frame_options(args: argparse.Namespace) -> VideoFrameOptions:
    return VideoFrameOptions(
        input_path=args.video_path,
        output_dir=args.output_dir,
        image_format=args.image_format,
        size=args.size,
        fps=args.fps,
        frames=args.frames,
        overwrite=args.overwrite,
    )


def _media_resize_options(args: argparse.Namespace) -> MediaResizeOptions:
    return MediaResizeOptions(
        input_path=args.input_path,
        size=args.size,
        output_path=args.output_path,
    )


def _system_exit_code(exc: SystemExit) -> int:
    if isinstance(exc.code, int):
        return exc.code
    if exc.code is None:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: 运行 CLI 测试确认通过**

Run:

```powershell
uv run pytest tests/test_main.py -v
```

Expected:

```text
All tests in tests/test_main.py pass.
```

- [ ] **Step 6: 运行相关测试确认集成通过**

Run:

```powershell
uv run pytest tests/test_main.py tests/test_media_resize.py tests/test_video_frames.py tests/test_ffmpeg_backend.py -v
```

Expected:

```text
All selected tests pass.
```

- [ ] **Step 7: 提交 CLI 接入**

Run:

```powershell
git add src/designtoolkit/main.py tests/test_main.py
git commit -m "feat: add resize media cli"
```

Expected:

```text
Git creates a commit with message: feat: add resize media cli
```

## Task 3: 更新 README 并完成验证

**Files:**
- Modify: `README.md`

- [ ] **Step 1: 在 README 中写使用说明**

在 `README.md` 的“视频转序列帧”章节后、“验证”章节前加入：

```markdown
## 修改视频或图片尺寸

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
```

- [ ] **Step 2: 运行 README 相关 smoke check**

Run:

```powershell
uv run designtoolkit resize-media --help
```

Expected:

```text
usage: designtoolkit resize-media
input_path
size
output_path
```

- [ ] **Step 3: 运行完整测试**

Run:

```powershell
uv run pytest
```

Expected:

```text
All tests pass.
```

- [ ] **Step 4: 运行 Ruff**

Run:

```powershell
uv run ruff check .
```

Expected:

```text
All checks passed!
```

- [ ] **Step 5: 运行 compileall**

Run:

```powershell
uv run python -m compileall src tests
```

Expected:

```text
No syntax errors. Command exits with code 0.
```

- [ ] **Step 6: 检查工作区变更**

Run:

```powershell
git status --short
git diff --stat
```

Expected:

```text
Only resize-media implementation, tests, and README changes are present.
No unrelated user changes are reverted.
```

- [ ] **Step 7: 提交文档和最终验证**

Run:

```powershell
git add README.md
git commit -m "docs: document resize media cli"
```

Expected:

```text
Git creates a commit with message: docs: document resize media cli
```

## Self-Review

Spec coverage:

- CLI 命令接口由 Task 2 覆盖。
- 默认输出路径由 Task 1 覆盖。
- 保持比例 FFmpeg scale filter 由 Task 1 覆盖。
- 复用 bundled FFmpeg 后端由 Task 2 默认 backend 测试覆盖。
- 安全不覆盖由 Task 1 输出路径存在测试覆盖。
- README 文档由 Task 3 覆盖。
- 最终验证命令由 Task 3 覆盖。

占位内容扫描:

- 文档没有遗留待补内容。
- 每个代码修改步骤都提供了具体代码块。
- 每个测试步骤都提供了具体命令和预期结果。

Type consistency:

- `MediaResizeOptions`、`MediaResizeResult`、`MediaResizeError`、`ResizeBackendResult` 在 Task 1 定义，并在 Task 2 中以相同名称导入使用。
- `resize_media()` 签名在 Task 1 和 Task 2 中保持一致。
- CLI 注入参数名统一为 `media_resizer`。
