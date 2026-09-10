from __future__ import annotations

import os
import re
from pathlib import Path
from stat import S_ISREG
from threading import RLock
from typing import BinaryIO

from designtoolkit.media_constraints import HOSTED_MAX_FRAME_OUTPUTS

type FileIdentity = tuple[str, int, int] | tuple[str, Path]

_TEMPORARY_NAME = re.compile(r"\..+\.[0-9a-f]{32}\.tmp\Z")


class OutputLimitExceeded(RuntimeError):
    """Raised when generated task output exceeds its configured budget."""


class OutputBudgetGuard:
    def __init__(
        self,
        root: Path,
        max_bytes: int,
        safety_bytes: int = 1,
        max_files: int = HOSTED_MAX_FRAME_OUTPUTS,
    ) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must not be negative")
        if safety_bytes < 0:
            raise ValueError("safety_bytes must not be negative")
        if max_files < 1:
            raise ValueError("max_files must be positive")
        self.root = Path(root).resolve()
        self.max_bytes = max_bytes
        self.safety_bytes = safety_bytes
        self.max_files = max_files
        self._used_bytes = 0
        self._sizes: dict[FileIdentity, int] = {}
        self._path_identities: dict[Path, FileIdentity] = {}
        self._created_identities: set[FileIdentity] = set()
        self._lock = RLock()

    @property
    def used_bytes(self) -> int:
        with self._lock:
            return self._used_bytes

    @property
    def remaining_bytes(self) -> int:
        with self._lock:
            return max(
                self.max_bytes - self.safety_bytes - self._used_bytes,
                0,
            )

    def check_health(self) -> None:
        pass

    def refresh_health(self, *, force: bool = False) -> None:
        del force
        self.check_health()

    def validate_path(self, path: str | Path) -> Path:
        with self._lock:
            return self._contained_path(Path(path))

    def account(self, path: Path) -> None:
        self.check_health()
        with self._lock:
            resolved = self._contained_path(path)
            stat_result = resolved.stat()
            if not S_ISREG(stat_result.st_mode):
                raise ValueError(f"Output path is not a file: {resolved}")
            identity = self._identity(
                resolved,
                stat_result.st_dev,
                stat_result.st_ino,
            )
            self._enforce_file_count(identity)
            previous_size = self._sizes.get(identity, 0)
            self._used_bytes += max(stat_result.st_size - previous_size, 0)
            self._sizes[identity] = stat_result.st_size
            self._path_identities[resolved] = identity
            self._enforce(self._used_bytes)

    def register_created(self, file: BinaryIO, path: str | Path) -> None:
        self.check_health()
        with self._lock:
            resolved = self._contained_path(Path(path))
            stat_result = os.fstat(file.fileno())
            if not S_ISREG(stat_result.st_mode):
                raise ValueError(f"Output path is not a file: {resolved}")
            identity = self._identity(
                resolved,
                stat_result.st_dev,
                stat_result.st_ino,
            )
            self._enforce_file_count(identity)
            previous_size = self._sizes.get(identity, 0)
            growth = max(stat_result.st_size - previous_size, 0)
            self._enforce(self._used_bytes + growth)
            self._used_bytes += growth
            self._sizes[identity] = max(previous_size, stat_result.st_size)
            self._path_identities[resolved] = identity
            self._created_identities.add(identity)

    def write(
        self,
        file: BinaryIO,
        path: str | Path,
        data: bytes | bytearray | memoryview,
    ) -> int:
        self.check_health()
        with self._lock:
            resolved = self._contained_path(Path(path))
            stat_result = os.fstat(file.fileno())
            if not S_ISREG(stat_result.st_mode):
                raise ValueError(f"Output path is not a file: {resolved}")
            identity = self._identity(
                resolved,
                stat_result.st_dev,
                stat_result.st_ino,
            )
            position = file.tell()
            if position < 0:
                raise ValueError("Output file position must not be negative")

            previous_size = self._sizes.get(identity, 0)
            observed_extent = stat_result.st_size
            baseline_growth = max(observed_extent - previous_size, 0)
            tracked_extent = max(previous_size, observed_extent)
            requested_extent = max(tracked_extent, position + len(data))
            requested_growth = requested_extent - tracked_extent
            self._enforce(
                self._used_bytes + baseline_growth + requested_growth
            )

            self._used_bytes += baseline_growth
            self._sizes[identity] = tracked_extent
            self._path_identities[resolved] = identity
            try:
                written = file.write(data)
            except BaseException:
                self._account_open_file(file, resolved, identity)
                raise
            minimum_extent = (
                position + written
                if isinstance(written, int) and 0 <= written <= len(data)
                else 0
            )
            self._account_open_file(
                file,
                resolved,
                identity,
                minimum_extent=minimum_extent,
            )
            if not isinstance(written, int) or written < 0 or written > len(data):
                raise OSError("Output writer returned an invalid byte count")
            return written

    def publish(self, source: str | Path, target: str | Path) -> None:
        self.check_health()
        with self._lock:
            resolved_source = self._temporary_path(Path(source))
            resolved_target = self._contained_path(Path(target))
            identity = self._path_identities.get(resolved_source)
            if (
                identity is None
                or identity not in self._created_identities
            ):
                raise ValueError(
                    "Publication source is not a registered temporary file: "
                    f"{resolved_source}"
                )
            try:
                os.link(resolved_source, resolved_target)
            except BaseException:
                self._discard_publication_source(resolved_source)
                raise
            self._path_identities[resolved_target] = identity
            try:
                resolved_source.unlink()
                self._path_identities.pop(resolved_source, None)
                self._transfer_published_identity(
                    identity,
                    resolved_target,
                )
                _fsync_parent_directory(resolved_target.parent)
            except BaseException:
                self._discard_publication_source(resolved_source)
                self._transfer_published_identity(
                    identity,
                    resolved_target,
                )
                raise

    def truncate(
        self,
        file: BinaryIO,
        path: str | Path,
        size: int | None = None,
    ) -> int:
        self.check_health()
        with self._lock:
            resolved = self._contained_path(Path(path))
            stat_result = os.fstat(file.fileno())
            identity = self._identity(
                resolved,
                stat_result.st_dev,
                stat_result.st_ino,
            )
            requested_extent = file.tell() if size is None else size
            if requested_extent < 0:
                raise ValueError("Output file size must not be negative")
            previous_size = self._sizes.get(identity, 0)
            observed_extent = stat_result.st_size
            baseline_growth = max(observed_extent - previous_size, 0)
            tracked_extent = max(previous_size, observed_extent)
            requested_growth = max(requested_extent - tracked_extent, 0)
            self._enforce(
                self._used_bytes + baseline_growth + requested_growth
            )
            self._used_bytes += baseline_growth
            self._sizes[identity] = tracked_extent
            self._path_identities[resolved] = identity
            try:
                result = file.truncate(size)
            except BaseException:
                self._account_open_file(file, resolved, identity)
                raise
            self._account_open_file(
                file,
                resolved,
                identity,
                minimum_extent=requested_extent,
            )
            return result

    def discard_temporary(self, path: str | Path) -> None:
        with self._lock:
            resolved = self._temporary_path(Path(path))
            identity = self._path_identities.get(resolved)
            if identity is None or identity not in self._created_identities:
                return
            try:
                stat_result = resolved.lstat()
            except FileNotFoundError:
                self._release_created_path(resolved, identity)
                return
            if not S_ISREG(stat_result.st_mode):
                self._release_created_path(resolved, identity)
                return
            current_identity = self._identity(
                resolved,
                stat_result.st_dev,
                stat_result.st_ino,
            )
            if identity[0] != "inode":
                return
            if current_identity != identity:
                self._release_created_path(resolved, identity)
                return
            try:
                resolved.unlink()
            except FileNotFoundError:
                pass
            self._release_created_path(resolved, identity)

    def _release_created_path(
        self,
        resolved: Path,
        identity: FileIdentity,
    ) -> None:
        self._path_identities.pop(resolved, None)
        for known_path, known_identity in tuple(
            self._path_identities.items()
        ):
            if known_identity != identity:
                continue
            try:
                stat_result = known_path.lstat()
            except FileNotFoundError:
                self._path_identities.pop(known_path, None)
                continue
            if not S_ISREG(stat_result.st_mode):
                self._path_identities.pop(known_path, None)
                continue
            if (
                identity[0] == "inode"
                and self._identity(
                    known_path,
                    stat_result.st_dev,
                    stat_result.st_ino,
                )
                != identity
            ):
                self._path_identities.pop(known_path, None)
        if identity in self._path_identities.values():
            return
        self._used_bytes -= self._sizes.pop(identity, 0)
        self._created_identities.discard(identity)

    def check_tree(self) -> None:
        self.check_health()
        if not self.root.exists():
            with self._lock:
                self._used_bytes = 0
                self._sizes.clear()
                self._path_identities.clear()
                self._created_identities.clear()
            return
        current_sizes: dict[FileIdentity, int] = {}
        current_paths: dict[Path, FileIdentity] = {}
        with self._lock:
            for path in self.root.rglob("*"):
                resolved = self._contained_path(path)
                if not resolved.is_file():
                    continue
                stat_result = resolved.stat()
                identity = self._identity(
                    resolved,
                    stat_result.st_dev,
                    stat_result.st_ino,
                )
                current_sizes[identity] = stat_result.st_size
                current_paths[resolved] = identity
            self._used_bytes = sum(current_sizes.values())
            self._sizes = current_sizes
            self._path_identities = current_paths
            self._created_identities.intersection_update(current_sizes)
            if len(current_sizes) > self.max_files:
                raise OutputLimitExceeded(
                    f"Task output exceeds the {self.max_files}-file limit"
                )
            self._enforce(self._used_bytes)

    def _enforce_file_count(self, identity: FileIdentity) -> None:
        if identity not in self._sizes and len(self._sizes) >= self.max_files:
            raise OutputLimitExceeded(
                f"Task output exceeds the {self.max_files}-file limit"
            )

    def _account_open_file(
        self,
        file: BinaryIO,
        resolved: Path,
        identity: FileIdentity,
        *,
        minimum_extent: int = 0,
    ) -> None:
        actual_extent = max(
            os.fstat(file.fileno()).st_size,
            file.tell(),
            minimum_extent,
        )
        previous_size = self._sizes.get(identity, 0)
        self._used_bytes += max(actual_extent - previous_size, 0)
        self._sizes[identity] = max(previous_size, actual_extent)
        self._path_identities[resolved] = identity

    def _discard_publication_source(self, source: Path) -> None:
        self.discard_temporary(source)

    def _transfer_published_identity(
        self,
        identity: FileIdentity,
        target: Path,
    ) -> None:
        if identity[0] != "path":
            return
        target_identity: FileIdentity = ("path", target)
        size = self._sizes.pop(identity, 0)
        self._sizes[target_identity] = size
        if identity in self._created_identities:
            self._created_identities.remove(identity)
            self._created_identities.add(target_identity)
        for known_path, known_identity in tuple(
            self._path_identities.items()
        ):
            if known_identity == identity:
                self._path_identities[known_path] = target_identity

    def _contained_path(self, path: Path) -> Path:
        resolved = Path(path).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(
                f"Output path is outside output root: {resolved}"
            ) from exc
        return resolved

    def _contained_entry_path(self, path: Path) -> Path:
        absolute = Path(os.path.abspath(path))
        resolved = absolute.parent.resolve() / absolute.name
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(
                f"Output path is outside output root: {resolved}"
            ) from exc
        return resolved

    def _temporary_path(self, path: Path) -> Path:
        resolved = self._contained_entry_path(path)
        if _TEMPORARY_NAME.fullmatch(resolved.name) is None:
            raise ValueError(
                "Output path is not a unique hidden temporary file: "
                f"{resolved}"
            )
        return resolved

    @staticmethod
    def _identity(path: Path, device: int, inode: int) -> FileIdentity:
        if inode:
            return ("inode", device, inode)
        return ("path", path)

    def _enforce(self, observed_bytes: int) -> None:
        if observed_bytes + self.safety_bytes > self.max_bytes:
            raise OutputLimitExceeded(
                f"Task output exceeds the configured {self.max_bytes}-byte limit"
            )


def _fsync_parent_directory(parent: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class OutputTargetMonitor:
    def __init__(self, guard: OutputBudgetGuard, target: str | Path) -> None:
        self._guard = guard
        self._target = Path(target)
        self._sequence = _parse_sequential_pattern(str(target))
        self._tail_index = 1

    def poll(self) -> None:
        if self._sequence is None:
            self._account_if_present(self._target)
            return

        prefix, width, suffix = self._sequence
        tail = Path(f"{prefix}{self._tail_index:0{width}d}{suffix}")
        if not self._account_if_present(tail):
            return
        while True:
            successor = Path(f"{prefix}{self._tail_index + 1:0{width}d}{suffix}")
            if not self._account_if_present(successor):
                return
            self._tail_index += 1

    def finish(self) -> None:
        self._guard.check_tree()

    def _account_if_present(self, path: Path) -> bool:
        try:
            self._guard.account(path)
        except FileNotFoundError:
            return False
        return True


def _parse_sequential_pattern(
    value: str,
) -> tuple[str, int, str] | None:
    literal: list[str] = []
    marker: tuple[str, int] | None = None
    invalid_sequence_percent = False
    index = 0
    while index < len(value):
        if value[index] != "%":
            literal.append(value[index])
            index += 1
            continue
        if index + 1 < len(value) and value[index + 1] == "%":
            literal.append("%")
            index += 2
            continue
        marker_end, width = _sequential_marker_at(value, index)
        if marker_end is not None and width is not None and width > 0:
            if marker is None:
                marker = ("".join(literal), width)
                literal = []
                index = marker_end
                continue
            invalid_sequence_percent = True
            index = marker_end
            continue
        invalid_sequence_percent = True
        literal.append("%")
        index += 1
    if marker is None:
        return None
    if invalid_sequence_percent:
        raise ValueError("Sequential output pattern contains invalid percent markers")
    prefix, width = marker
    return prefix, width, "".join(literal)


def _sequential_marker_at(
    value: str,
    index: int,
) -> tuple[int | None, int | None]:
    zero_index = index + 1
    if zero_index >= len(value) or value[zero_index] != "0":
        return None, None
    width_start = zero_index + 1
    width_end = width_start
    while width_end < len(value) and value[width_end].isdigit():
        width_end += 1
    if (
        width_end == width_start
        or width_end >= len(value)
        or value[width_end] != "d"
    ):
        return None, None
    return width_end + 1, int(value[width_start:width_end])
