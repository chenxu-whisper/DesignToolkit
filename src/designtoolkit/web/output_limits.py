from __future__ import annotations

from pathlib import Path
from stat import S_ISREG
from threading import RLock

type FileIdentity = tuple[str, int, int] | tuple[str, Path]


class OutputLimitExceeded(RuntimeError):
    """Raised when generated task output exceeds its configured budget."""


class OutputBudgetGuard:
    def __init__(
        self,
        root: Path,
        max_bytes: int,
        safety_bytes: int = 1,
    ) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must not be negative")
        if safety_bytes < 0:
            raise ValueError("safety_bytes must not be negative")
        self.root = Path(root).resolve()
        self.max_bytes = max_bytes
        self.safety_bytes = safety_bytes
        self._used_bytes = 0
        self._sizes: dict[FileIdentity, int] = {}
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

    def account(self, path: Path) -> None:
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
            previous_size = self._sizes.get(identity, 0)
            self._used_bytes += max(stat_result.st_size - previous_size, 0)
            self._sizes[identity] = stat_result.st_size
            self._enforce(self._used_bytes)

    def check_tree(self) -> None:
        if not self.root.exists():
            return
        current_sizes: dict[FileIdentity, int] = {}
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
                previous_size = self._sizes.get(identity, 0)
                self._used_bytes += max(stat_result.st_size - previous_size, 0)
                self._sizes[identity] = stat_result.st_size
            self._enforce(max(self._used_bytes, sum(current_sizes.values())))

    def _contained_path(self, path: Path) -> Path:
        resolved = Path(path).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(
                f"Output path is outside output root: {resolved}"
            ) from exc
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


class OutputTargetMonitor:
    def __init__(self, guard: OutputBudgetGuard, target: str | Path) -> None:
        self._guard = guard
        self._target = Path(target)
        self._sequence = _parse_sequential_pattern(str(target))
        self._next_index = 1

    def poll(self) -> None:
        if self._sequence is None:
            self._account_if_present(self._target)
            return

        prefix, width, suffix = self._sequence
        while True:
            candidate = Path(
                f"{prefix}{self._next_index:0{width}d}{suffix}"
            )
            if not self._account_if_present(candidate):
                return
            self._next_index += 1

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
        width_start = index + 1
        if width_start < len(value) and value[width_start] == "0":
            width_start += 1
            width_end = width_start
            while width_end < len(value) and value[width_end].isdigit():
                width_end += 1
            if (
                width_end > width_start
                and width_end < len(value)
                and value[width_end] == "d"
                and marker is None
            ):
                marker = ("".join(literal), int(value[width_start:width_end]))
                literal = []
                index = width_end + 1
                continue
        return None
    if marker is None:
        return None
    prefix, width = marker
    return prefix, width, "".join(literal)
