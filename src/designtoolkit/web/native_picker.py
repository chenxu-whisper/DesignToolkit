from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol

from designtoolkit.web.local_paths import default_local_path_roots

LocalFileKind = Literal["video", "media", "image", "any"]
_VIDEO_FILE_SUFFIXES = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v")
_MEDIA_IMAGE_FILE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
_IMAGE_FILE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})
NATIVE_PICKER_VIDEO_SUFFIXES = frozenset(_VIDEO_FILE_SUFFIXES)
NATIVE_PICKER_IMAGE_SUFFIXES = _IMAGE_FILE_SUFFIXES
NATIVE_PICKER_MEDIA_SUFFIXES = NATIVE_PICKER_VIDEO_SUFFIXES | frozenset(
    _MEDIA_IMAGE_FILE_SUFFIXES
)


class LocalPathPickerError(RuntimeError):
    pass


class LocalPathPicker(Protocol):
    def pick_files(
        self,
        *,
        title: str,
        multiple: bool,
        file_kind: str,
        allowed_suffixes: list[str] | None = None,
    ) -> list[Path]:
        pass

    def pick_directory(self, *, title: str) -> list[Path]:
        pass

    def pick_save_file(
        self,
        *,
        title: str,
        file_kind: str,
    ) -> list[Path]:
        pass


class TkinterLocalPathPicker:
    def __init__(self) -> None:
        self._last_directory: Path | None = None

    def pick_files(
        self,
        *,
        title: str,
        multiple: bool,
        file_kind: str,
        allowed_suffixes: list[str] | None = None,
    ) -> list[Path]:
        root, filedialog = _create_tk_dialog_context()
        try:
            options = {
                "parent": root,
                "title": title,
                "filetypes": _filetypes(file_kind, allowed_suffixes),
                "initialdir": self._initial_directory(file_kind),
            }
            if multiple:
                selected = filedialog.askopenfilenames(**options)
                paths = [Path(path) for path in selected]
                paths = _filter_paths_by_file_kind(
                    paths,
                    file_kind,
                    allowed_suffixes,
                )
                self._remember_file_directory(paths)
                return paths
            selected = filedialog.askopenfilename(**options)
            paths = [Path(selected)] if selected else []
            paths = _filter_paths_by_file_kind(
                paths,
                file_kind,
                allowed_suffixes,
            )
            self._remember_file_directory(paths)
            return paths
        finally:
            root.destroy()

    def pick_directory(self, *, title: str) -> list[Path]:
        root, filedialog = _create_tk_dialog_context()
        try:
            selected = filedialog.askdirectory(
                parent=root,
                title=title,
                initialdir=self._initial_directory("any"),
            )
            paths = [Path(selected)] if selected else []
            self._remember_directory(paths)
            return paths
        finally:
            root.destroy()

    def pick_save_file(
        self,
        *,
        title: str,
        file_kind: str,
    ) -> list[Path]:
        root, filedialog = _create_tk_dialog_context()
        try:
            selected = filedialog.asksaveasfilename(
                parent=root,
                title=title,
                filetypes=_save_filetypes(file_kind),
                initialdir=self._initial_directory("any"),
                **_save_file_options(file_kind),
            )
            paths = [Path(selected)] if selected else []
            paths = _normalize_save_paths(paths, file_kind)
            self._remember_file_directory(paths)
            return paths
        finally:
            root.destroy()

    def _initial_directory(self, file_kind: str) -> str:
        if self._last_directory is not None:
            return str(self._last_directory)
        return _default_initial_directory(file_kind)

    def _remember_file_directory(self, paths: list[Path]) -> None:
        if paths:
            self._last_directory = paths[0].parent

    def _remember_directory(self, paths: list[Path]) -> None:
        if paths:
            self._last_directory = paths[0]


def _create_tk_dialog_context():
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # pragma: no cover - depends on local Python build
        raise LocalPathPickerError("System file picker is unavailable.") from exc

    try:
        root = tk.Tk()
        root.withdraw()
        root.update()
        try:
            root.attributes("-topmost", True)
        except tk.TclError:
            pass
    except Exception as exc:  # pragma: no cover - depends on desktop session
        raise LocalPathPickerError("System file picker could not be opened.") from exc
    return root, filedialog


def _filetypes(
    file_kind: str,
    allowed_suffixes: list[str] | None = None,
):
    if allowed_suffixes is not None:
        return _constrained_filetypes(file_kind, allowed_suffixes)

    video_types = _filetype_pattern(_VIDEO_FILE_SUFFIXES)
    image_types = _filetype_pattern(_MEDIA_IMAGE_FILE_SUFFIXES)
    if file_kind == "video":
        return [("Video files", video_types), ("All files", "*.*")]
    if file_kind == "image":
        return [("Image files", "*.png *.jpg *.jpeg")]
    if file_kind == "media":
        return [
            ("Media files", f"{video_types} {image_types}"),
            ("Video files", video_types),
            ("Image files", image_types),
            ("All files", "*.*"),
        ]
    return [("All files", "*.*")]


def _constrained_filetypes(
    file_kind: str,
    allowed_suffixes: list[str],
) -> list[tuple[str, str]]:
    suffixes = _normalize_suffixes(allowed_suffixes)
    allowed_types = _filetype_pattern(suffixes)
    if file_kind == "video":
        return [("Video files", allowed_types)]
    if file_kind == "image":
        return [("Image files", allowed_types)]
    if file_kind == "media":
        video_suffixes = tuple(
            suffix for suffix in suffixes if suffix in _VIDEO_FILE_SUFFIXES
        )
        image_suffixes = tuple(
            suffix for suffix in suffixes if suffix in _MEDIA_IMAGE_FILE_SUFFIXES
        )
        filetypes = [("Media files", allowed_types)]
        if video_suffixes:
            filetypes.append(("Video files", _filetype_pattern(video_suffixes)))
        if image_suffixes:
            filetypes.append(("Image files", _filetype_pattern(image_suffixes)))
        return filetypes
    return [("Allowed files", allowed_types)]


def _normalize_suffixes(suffixes: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(suffix.lower() for suffix in suffixes))


def _filetype_pattern(suffixes: tuple[str, ...]) -> str:
    return " ".join(f"*{suffix}" for suffix in suffixes)


def _save_filetypes(file_kind: str):
    if file_kind == "video":
        return [("MP4 files", "*.mp4")]
    if file_kind == "image":
        return [("PNG files", "*.png")]
    return _filetypes(file_kind)


def _save_file_options(file_kind: str) -> dict[str, str]:
    if file_kind == "video":
        return {"defaultextension": ".mp4"}
    if file_kind == "image":
        return {"defaultextension": ".png"}
    return {}


def _filter_paths_by_file_kind(
    paths: list[Path],
    file_kind: str,
    allowed_suffixes: list[str] | None = None,
) -> list[Path]:
    if allowed_suffixes is not None:
        allowed = frozenset(_normalize_suffixes(allowed_suffixes))
        return [path for path in paths if path.suffix.lower() in allowed]
    if file_kind == "image":
        return [path for path in paths if path.suffix.lower() in _IMAGE_FILE_SUFFIXES]
    return paths


def _normalize_save_paths(paths: list[Path], file_kind: str) -> list[Path]:
    if file_kind == "video":
        return [
            path.with_suffix(".mp4") if not path.suffix else path
            for path in paths
            if not path.suffix or path.suffix.lower() == ".mp4"
        ]
    if file_kind != "image":
        return paths

    normalized_paths = []
    for path in paths:
        if not path.suffix:
            normalized_paths.append(path.with_suffix(".png"))
        elif path.suffix.lower() == ".png":
            normalized_paths.append(path)
    return normalized_paths


def _default_initial_directory(file_kind: str) -> str:
    roots = default_local_path_roots()
    if file_kind == "video":
        for root in roots:
            if root.name.lower() == "video" and root.parent.name.lower() == "assets":
                return str(root)
    return str(roots[0]) if roots else str(Path.cwd())
