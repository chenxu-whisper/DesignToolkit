from __future__ import annotations

import ntpath
import os
import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from designtoolkit.web.schemas import LogEventResponse

MANAGED_PATH_PLACEHOLDER = "[managed path]"
_INPUT_FIELDS = ("input_path", "input_paths")
_OUTPUT_FILE_FIELDS = ("output_path",)
_OUTPUT_ROOT_FIELDS = ("output_dir", "output_root")
_MAX_DECODE_ROUNDS = 4
_OPAQUE_DIRECTORY_NAME = re.compile(
    r"(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_CANDIDATE_TERMINATORS = frozenset("\r\n'\"<>|,;)]}&#")


@dataclass(frozen=True)
class _TaskPaths:
    inputs: frozenset[str]
    output_files: frozenset[str]
    output_roots: frozenset[str]


class EventSanitizer:
    def __init__(self, data_dir: Path) -> None:
        lexical_path = Path(os.path.abspath(data_dir))
        try:
            real_path = data_dir.resolve(strict=False)
        except OSError:
            real_path = lexical_path
        roots = (
            _canonical_path(str(lexical_path)),
            _canonical_path(str(real_path)),
        )
        self._managed_roots = tuple(
            dict.fromkeys(root for root in roots if _is_absolute_path(root))
        )

    def sanitize_event(
        self,
        event: LogEventResponse,
        *,
        task_payload: Mapping[str, object] | None,
    ) -> LogEventResponse:
        return event.model_copy(
            update={
                "message": self.sanitize_text(
                    event.message,
                    task_payload=task_payload,
                ),
                "details": self.sanitize_details(
                    event.details,
                    task_payload=task_payload,
                ),
            }
        )

    def sanitize_text(
        self,
        value: str,
        *,
        task_payload: Mapping[str, object] | None,
    ) -> str:
        if not self._managed_roots:
            return value
        paths = _task_paths(
            task_payload,
            managed_roots=self._managed_roots,
        )
        direct_display = _display_path_value(
            _canonical_path(value),
            managed_roots=self._managed_roots,
            paths=paths,
        )
        if direct_display is not None:
            return direct_display
        return _replace_path_candidates(
            value,
            managed_roots=self._managed_roots,
            paths=paths,
        )

    def sanitize_details(
        self,
        value: dict[str, Any],
        *,
        task_payload: Mapping[str, object] | None,
    ) -> dict[str, Any]:
        sanitized = self._sanitize_nested(
            value,
            task_payload=task_payload,
        )
        if not isinstance(sanitized, dict):
            raise TypeError("Event details must remain a dictionary")
        return sanitized

    def _sanitize_nested(
        self,
        value: Any,
        *,
        task_payload: Mapping[str, object] | None,
    ) -> Any:
        if isinstance(value, str):
            return self.sanitize_text(
                value,
                task_payload=task_payload,
            )
        if isinstance(value, dict):
            return {
                self._sanitize_nested(
                    key,
                    task_payload=task_payload,
                ): self._sanitize_nested(
                    item,
                    task_payload=task_payload,
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                self._sanitize_nested(
                    item,
                    task_payload=task_payload,
                )
                for item in value
            ]
        if isinstance(value, tuple):
            return tuple(
                self._sanitize_nested(
                    item,
                    task_payload=task_payload,
                )
                for item in value
            )
        return value


def sanitize_event_text(
    value: str,
    *,
    data_dir: Path,
    task_payload: Mapping[str, object] | None,
) -> str:
    return EventSanitizer(data_dir).sanitize_text(
        value,
        task_payload=task_payload,
    )


def sanitize_event_details(
    value: dict[str, Any],
    *,
    data_dir: Path,
    task_payload: Mapping[str, object] | None,
) -> dict[str, Any]:
    return EventSanitizer(data_dir).sanitize_details(
        value,
        task_payload=task_payload,
    )


def _task_paths(
    payload: Mapping[str, object] | None,
    *,
    managed_roots: tuple[str, ...],
) -> _TaskPaths:
    if payload is None:
        return _TaskPaths(frozenset(), frozenset(), frozenset())
    return _TaskPaths(
        inputs=_field_paths(
            payload,
            _INPUT_FIELDS,
            managed_roots=managed_roots,
        ),
        output_files=_field_paths(
            payload,
            _OUTPUT_FILE_FIELDS,
            managed_roots=managed_roots,
        ),
        output_roots=_field_paths(
            payload,
            _OUTPUT_ROOT_FIELDS,
            managed_roots=managed_roots,
        ),
    )


def _field_paths(
    payload: Mapping[str, object],
    fields: tuple[str, ...],
    *,
    managed_roots: tuple[str, ...],
) -> frozenset[str]:
    paths: set[str] = set()
    for field in fields:
        value = payload.get(field)
        values = value if isinstance(value, list) else [value]
        for candidate in values:
            if not isinstance(candidate, str):
                continue
            canonical = _canonical_path(candidate)
            aliases = _managed_aliases(canonical, managed_roots)
            paths.update(aliases)
    return frozenset(paths)


def _managed_aliases(
    candidate: str,
    managed_roots: tuple[str, ...],
) -> tuple[str, ...]:
    if not _is_absolute_path(candidate):
        return ()
    for source_root in managed_roots:
        relative = _relative_path(candidate, source_root)
        if relative is None:
            continue
        return tuple(_join_canonical(root, relative) for root in managed_roots)
    return ()


def _join_canonical(root: str, relative: str) -> str:
    if not relative:
        return root
    return f"{root.rstrip('/')}/{relative}"


def _replace_path_candidates(
    value: str,
    *,
    managed_roots: tuple[str, ...],
    paths: _TaskPaths,
) -> str:
    result: list[str] = []
    copied_through = 0
    index = 0
    while index < len(value):
        if not _looks_like_candidate_start(value, index):
            index += 1
            continue
        replacement = _candidate_replacement(
            value,
            index,
            managed_roots=managed_roots,
            paths=paths,
        )
        if replacement is None:
            index += 1
            continue
        end, display = replacement
        result.append(value[copied_through:index])
        result.append(display)
        copied_through = end
        index = end
    if not result:
        return value
    result.append(value[copied_through:])
    return "".join(result)


def _looks_like_candidate_start(value: str, index: int) -> bool:
    if index > 0 and (value[index - 1].isalnum() or value[index - 1] == "%"):
        return False
    prefix = _decode_path(value[index : index + 64]).replace("\\", "/")
    return bool(
        prefix.startswith("/")
        or prefix.casefold().startswith("file:")
        or re.match(r"^[A-Za-z]:/", prefix)
    )


def _candidate_replacement(
    value: str,
    start: int,
    *,
    managed_roots: tuple[str, ...],
    paths: _TaskPaths,
) -> tuple[int, str] | None:
    hard_end = start
    while hard_end < len(value) and value[hard_end] not in _CANDIDATE_TERMINATORS:
        hard_end += 1

    boundaries = [
        end
        for end in range(start + 1, hard_end + 1)
        if end == hard_end
        or value[end].isspace()
        or value[end] in _CANDIDATE_TERMINATORS
    ]
    exact_match: tuple[int, str] | None = None
    for end in boundaries:
        display = _display_exact_path(
            _canonical_path(value[start:end]),
            paths=paths,
        )
        if display is not None:
            exact_match = end, display
    if exact_match is not None:
        return exact_match

    fallback: tuple[int, str] | None = None
    for end in boundaries:
        candidate = _canonical_path(value[start:end])
        display = _display_path_value(
            candidate,
            managed_roots=managed_roots,
            paths=paths,
        )
        if display is None:
            continue
        fallback = end, display
        if (
            display != MANAGED_PATH_PLACEHOLDER
            or any(_paths_equal(candidate, root) for root in managed_roots)
            or _looks_like_file_path(candidate)
        ):
            return end, display
    return fallback


def _display_exact_path(
    candidate: str,
    *,
    paths: _TaskPaths,
) -> str | None:
    if not _is_absolute_path(candidate):
        return None
    for path in paths.inputs:
        if _paths_equal(candidate, path):
            return _basename(path)
    for path in paths.output_files:
        if _paths_equal(candidate, path):
            return _safe_output_name(path)
    for path in paths.output_roots:
        if _paths_equal(candidate, path):
            return _safe_output_name(path)
    return None


def _display_path_value(
    candidate: str,
    *,
    managed_roots: tuple[str, ...],
    paths: _TaskPaths,
) -> str | None:
    exact = _display_exact_path(candidate, paths=paths)
    if exact is not None:
        return exact
    if not _is_absolute_path(candidate):
        return None
    for path in paths.output_roots:
        relative = _relative_path(candidate, path)
        if relative is None:
            continue
        if not relative:
            return _safe_output_name(path)
        parts = relative.split("/")
        if any(_OPAQUE_DIRECTORY_NAME.fullmatch(part) for part in parts):
            return MANAGED_PATH_PLACEHOLDER
        return "/".join(parts)
    if any(_relative_path(candidate, root) is not None for root in managed_roots):
        return MANAGED_PATH_PLACEHOLDER
    return None


def _decode_path(value: str) -> str:
    decoded = value
    for _ in range(_MAX_DECODE_ROUNDS):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def _canonical_path(value: str) -> str:
    decoded = _decode_path(value)
    uri_candidate = decoded.replace("\\", "/")
    if uri_candidate.casefold().startswith("file:"):
        parsed = urlsplit(uri_candidate)
        if parsed.netloc and parsed.netloc.casefold() != "localhost":
            decoded = f"//{parsed.netloc}{parsed.path}"
        else:
            decoded = parsed.path
    normalized = decoded.replace("\\", "/")
    if re.match(r"^/[A-Za-z]:/", normalized):
        normalized = normalized[1:]
    if _is_windows_path(normalized):
        return ntpath.normpath(normalized).replace("\\", "/")
    if normalized.startswith("/"):
        return posixpath.normpath(normalized)
    return normalized


def _paths_equal(left: str, right: str) -> bool:
    if _is_windows_path(left) and _is_windows_path(right):
        return left.casefold() == right.casefold()
    return left == right


def _relative_path(candidate: str, root: str) -> str | None:
    normalized_root = root.rstrip("/")
    prefix = normalized_root + "/"
    comparison_candidate = candidate
    comparison_root = normalized_root
    comparison_prefix = prefix
    if _is_windows_path(candidate) and _is_windows_path(root):
        comparison_candidate = candidate.casefold()
        comparison_root = normalized_root.casefold()
        comparison_prefix = prefix.casefold()
    if comparison_candidate == comparison_root:
        return ""
    if not comparison_candidate.startswith(comparison_prefix):
        return None
    return candidate[len(prefix) :]


def _is_absolute_path(path: str) -> bool:
    return path.startswith("/") or bool(re.match(r"^[A-Za-z]:/", path))


def _is_windows_path(path: str) -> bool:
    return path.startswith("//") or bool(re.match(r"^[A-Za-z]:/", path))


def _looks_like_file_path(path: str) -> bool:
    name = _basename(path)
    return "." in name and not name.endswith(".")


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def _safe_output_name(path: str) -> str:
    name = _basename(path)
    return "output" if _OPAQUE_DIRECTORY_NAME.fullmatch(name) else name
