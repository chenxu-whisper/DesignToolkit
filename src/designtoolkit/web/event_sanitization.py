from __future__ import annotations

import ntpath
import os
import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal
from urllib.parse import urlsplit

from designtoolkit.web.schemas import LogEventResponse

MANAGED_PATH_PLACEHOLDER = "[managed path]"
PUBLIC_EVENT_TRUNCATED_MARKER = " [truncated]"
MAX_PUBLIC_EVENT_STRING_LENGTH = 8 * 1024
MAX_PUBLIC_EVENT_DEPTH = 16
MAX_PUBLIC_EVENT_NODES = 1024
_INPUT_FIELDS = ("input_path", "input_paths")
_OUTPUT_FILE_FIELDS = ("output_path",)
_OUTPUT_ROOT_FIELDS = ("output_dir", "output_root")
_MAX_TASK_PATH_CANDIDATES = 128
_MAX_TASK_PATH_CHARACTERS = 64 * 1024
_OPAQUE_DIRECTORY_NAME = re.compile(
    r"(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_CANDIDATE_TERMINATORS = frozenset("\r\n'\"<>|,;)]}&#?")
_PathStyle = Literal["posix", "windows"]


@dataclass(frozen=True)
class _TaskPaths:
    inputs: frozenset[str]
    output_files: frozenset[str]
    output_roots: frozenset[str]


@dataclass(frozen=True)
class _DecodedText:
    value: str
    source_spans: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class _TextReplacement:
    start: int
    end: int
    value: str


@dataclass
class _TraversalBudget:
    remaining_nodes: int = MAX_PUBLIC_EVENT_NODES

    def consume(self) -> bool:
        if self.remaining_nodes <= 0:
            return False
        self.remaining_nodes -= 1
        return True


class EventSanitizer:
    def __init__(self, data_dir: Path | PurePosixPath | PureWindowsPath) -> None:
        self._path_style = _path_style_for(data_dir)
        raw_path = os.fspath(data_dir)
        is_non_native_pure_path = not isinstance(data_dir, Path)
        if is_non_native_pure_path and _is_absolute_path(
            raw_path,
            path_style=self._path_style,
        ):
            lexical_value = raw_path
            real_value = raw_path
        else:
            lexical_path = Path(os.path.abspath(data_dir))
            try:
                real_path = Path(data_dir).resolve(strict=False)
            except OSError:
                real_path = lexical_path
            lexical_value = str(lexical_path)
            real_value = str(real_path)
        roots = (
            _canonical_path(lexical_value, path_style=self._path_style),
            _canonical_path(real_value, path_style=self._path_style),
        )
        self._managed_roots = tuple(
            dict.fromkeys(
                root
                for root in roots
                if _is_absolute_path(root, path_style=self._path_style)
            )
        )

    def sanitize_event(
        self,
        event: LogEventResponse,
        *,
        task_payload: Mapping[str, object] | None,
    ) -> LogEventResponse:
        paths = self._task_paths(task_payload)
        budget = _TraversalBudget()
        return event.model_copy(
            update={
                "message": self._sanitize_text(
                    event.message,
                    paths=paths,
                ),
                "details": self._sanitize_details(
                    event.details,
                    paths=paths,
                    budget=budget,
                ),
            }
        )

    def sanitize_text(
        self,
        value: str,
        *,
        task_payload: Mapping[str, object] | None,
    ) -> str:
        return self._sanitize_text(
            value,
            paths=self._task_paths(task_payload),
        )

    def _sanitize_text(
        self,
        value: str,
        *,
        paths: _TaskPaths,
    ) -> str:
        value = _truncate_public_string(value)
        if not self._managed_roots:
            return value
        decoded = _decode_text(value)
        known_replacements = _known_path_replacements(
            decoded.value,
            paths=paths,
            path_style=self._path_style,
        )
        candidate_replacements = _path_candidate_replacements(
            decoded.value,
            managed_roots=self._managed_roots,
            paths=paths,
            path_style=self._path_style,
            blocked=known_replacements,
        )
        if candidate_replacements is None:
            return MANAGED_PATH_PLACEHOLDER
        return _apply_text_replacements(
            value,
            decoded=decoded,
            replacements=(*known_replacements, *candidate_replacements),
        )

    def sanitize_details(
        self,
        value: dict[str, Any],
        *,
        task_payload: Mapping[str, object] | None,
    ) -> dict[str, Any]:
        return self._sanitize_details(
            value,
            paths=self._task_paths(task_payload),
            budget=_TraversalBudget(),
        )

    def _sanitize_details(
        self,
        value: dict[str, Any],
        *,
        paths: _TaskPaths,
        budget: _TraversalBudget,
    ) -> dict[str, Any]:
        sanitized = self._sanitize_nested(
            value,
            paths=paths,
            budget=budget,
            depth=0,
        )
        if not isinstance(sanitized, dict):
            raise TypeError("Event details must remain a dictionary")
        return sanitized

    def _sanitize_nested(
        self,
        value: Any,
        *,
        paths: _TaskPaths,
        budget: _TraversalBudget,
        depth: int,
    ) -> Any:
        if not budget.consume():
            return PUBLIC_EVENT_TRUNCATED_MARKER
        if isinstance(value, str):
            return self._sanitize_text(
                value,
                paths=paths,
            )
        if depth >= MAX_PUBLIC_EVENT_DEPTH and isinstance(
            value,
            (dict, list, tuple),
        ):
            return PUBLIC_EVENT_TRUNCATED_MARKER
        if isinstance(value, dict):
            result: dict[Any, Any] = {}
            for key, item in value.items():
                if budget.remaining_nodes <= 0:
                    result[PUBLIC_EVENT_TRUNCATED_MARKER] = (
                        PUBLIC_EVENT_TRUNCATED_MARKER
                    )
                    break
                sanitized_key = self._sanitize_nested(
                    key,
                    paths=paths,
                    budget=budget,
                    depth=depth + 1,
                )
                sanitized_item = self._sanitize_nested(
                    item,
                    paths=paths,
                    budget=budget,
                    depth=depth + 1,
                )
                result[sanitized_key] = sanitized_item
            return result
        if isinstance(value, list):
            result_list: list[Any] = []
            for item in value:
                if budget.remaining_nodes <= 0:
                    result_list.append(PUBLIC_EVENT_TRUNCATED_MARKER)
                    break
                result_list.append(
                    self._sanitize_nested(
                        item,
                        paths=paths,
                        budget=budget,
                        depth=depth + 1,
                    )
                )
            return result_list
        if isinstance(value, tuple):
            result_tuple: list[Any] = []
            for item in value:
                if budget.remaining_nodes <= 0:
                    result_tuple.append(PUBLIC_EVENT_TRUNCATED_MARKER)
                    break
                result_tuple.append(
                    self._sanitize_nested(
                        item,
                        paths=paths,
                        budget=budget,
                        depth=depth + 1,
                    )
                )
            return tuple(result_tuple)
        return value

    def _task_paths(
        self,
        task_payload: Mapping[str, object] | None,
    ) -> _TaskPaths:
        return _task_paths(
            task_payload,
            managed_roots=self._managed_roots,
            path_style=self._path_style,
        )


def sanitize_event_text(
    value: str,
    *,
    data_dir: Path | PurePosixPath | PureWindowsPath,
    task_payload: Mapping[str, object] | None,
) -> str:
    return EventSanitizer(data_dir).sanitize_text(
        value,
        task_payload=task_payload,
    )


def sanitize_event_details(
    value: dict[str, Any],
    *,
    data_dir: Path | PurePosixPath | PureWindowsPath,
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
    path_style: _PathStyle,
) -> _TaskPaths:
    if payload is None:
        return _TaskPaths(frozenset(), frozenset(), frozenset())
    return _TaskPaths(
        inputs=_field_paths(
            payload,
            _INPUT_FIELDS,
            managed_roots=managed_roots,
            path_style=path_style,
        ),
        output_files=_field_paths(
            payload,
            _OUTPUT_FILE_FIELDS,
            managed_roots=managed_roots,
            path_style=path_style,
        ),
        output_roots=_field_paths(
            payload,
            _OUTPUT_ROOT_FIELDS,
            managed_roots=managed_roots,
            path_style=path_style,
        ),
    )


def _field_paths(
    payload: Mapping[str, object],
    fields: tuple[str, ...],
    *,
    managed_roots: tuple[str, ...],
    path_style: _PathStyle,
) -> frozenset[str]:
    paths: set[str] = set()
    characters = 0
    candidate_count = 0
    for field in fields:
        value = payload.get(field)
        values = value if isinstance(value, list) else [value]
        for candidate in values:
            if candidate_count >= _MAX_TASK_PATH_CANDIDATES:
                return frozenset(paths)
            candidate_count += 1
            if not isinstance(candidate, str):
                continue
            remaining = _MAX_TASK_PATH_CHARACTERS - characters
            if remaining <= 0:
                return frozenset(paths)
            bounded_candidate = candidate[
                : min(remaining, MAX_PUBLIC_EVENT_STRING_LENGTH)
            ]
            characters += len(bounded_candidate)
            canonical = _canonical_path(
                bounded_candidate,
                path_style=path_style,
            )
            aliases = _managed_aliases(
                canonical,
                managed_roots,
                path_style=path_style,
            )
            paths.update(aliases)
    return frozenset(paths)


def _managed_aliases(
    candidate: str,
    managed_roots: tuple[str, ...],
    *,
    path_style: _PathStyle,
) -> tuple[str, ...]:
    if not _is_absolute_path(candidate, path_style=path_style):
        return ()
    for source_root in managed_roots:
        relative = _relative_path(
            candidate,
            source_root,
            path_style=path_style,
        )
        if relative is None:
            continue
        return tuple(_join_canonical(root, relative) for root in managed_roots)
    return ()


def _join_canonical(root: str, relative: str) -> str:
    if not relative:
        return root
    return f"{root.rstrip('/')}/{relative}"


def _known_path_replacements(
    value: str,
    *,
    paths: _TaskPaths,
    path_style: _PathStyle,
) -> tuple[_TextReplacement, ...]:
    replacements = {
        **{path: _basename(path) for path in paths.inputs},
        **{path: _safe_output_name(path) for path in paths.output_files},
        **{path: _safe_output_name(path) for path in paths.output_roots},
    }
    matches: list[_TextReplacement] = []
    for path in sorted(replacements, key=len, reverse=True):
        expression = _known_path_expression(path, path_style=path_style)
        for match in re.finditer(
            expression + r"(?![\\/])",
            flags=_path_flags(path_style),
            string=value,
        ):
            matches.append(
                _TextReplacement(
                    start=match.start(),
                    end=match.end(),
                    value=replacements[path],
                )
            )

    selected: list[_TextReplacement] = []
    for replacement in sorted(
        matches,
        key=lambda item: (item.start, -(item.end - item.start)),
    ):
        if selected and replacement.start < selected[-1].end:
            continue
        selected.append(replacement)
    return tuple(selected)


def _path_candidate_replacements(
    value: str,
    *,
    managed_roots: tuple[str, ...],
    paths: _TaskPaths,
    path_style: _PathStyle,
    blocked: tuple[_TextReplacement, ...],
) -> tuple[_TextReplacement, ...] | None:
    result: list[_TextReplacement] = []
    blocked_index = 0
    candidate_starts = _managed_candidate_starts(
        value,
        managed_roots=managed_roots,
        path_style=path_style,
    )
    candidate_index = 0
    while candidate_index < len(candidate_starts):
        index = candidate_starts[candidate_index]
        while (
            blocked_index < len(blocked)
            and blocked[blocked_index].end <= index
        ):
            blocked_index += 1
        if (
            blocked_index < len(blocked)
            and blocked[blocked_index].start <= index < blocked[blocked_index].end
        ):
            candidate_index += 1
            continue
        replacement = _candidate_replacement(
            value,
            index,
            managed_roots=managed_roots,
            paths=paths,
            path_style=path_style,
        )
        if replacement is None:
            token_end = _candidate_end(value, index)
            candidate_index += 1
            if (
                candidate_index < len(candidate_starts)
                and candidate_starts[candidate_index] < token_end
            ):
                return None
            continue
        end, display, reliable = replacement
        if not reliable:
            return None
        if (
            blocked_index >= len(blocked)
            or blocked[blocked_index].start >= end
        ):
            result.append(
                _TextReplacement(start=index, end=end, value=display)
            )
        candidate_index += 1
        while (
            candidate_index < len(candidate_starts)
            and candidate_starts[candidate_index] < end
        ):
            candidate_index += 1
    return tuple(result)


def _managed_candidate_starts(
    value: str,
    *,
    managed_roots: tuple[str, ...],
    path_style: _PathStyle,
) -> tuple[int, ...]:
    starts: set[int] = set()
    for root in managed_roots:
        expression = _known_path_expression(root, path_style=path_style)
        for match in re.finditer(
            expression + r"(?=$|[\\/])",
            value,
            flags=_path_flags(path_style),
        ):
            starts.add(match.start())
    return tuple(sorted(starts))


def _apply_text_replacements(
    source: str,
    *,
    decoded: _DecodedText,
    replacements: tuple[_TextReplacement, ...],
) -> str:
    if not replacements:
        return source
    result: list[str] = []
    copied_through = 0
    for replacement in sorted(replacements, key=lambda item: item.start):
        source_start = decoded.source_spans[replacement.start][0]
        source_end = decoded.source_spans[replacement.end - 1][1]
        if source_start < copied_through:
            continue
        result.append(source[copied_through:source_start])
        result.append(replacement.value)
        copied_through = source_end
    result.append(source[copied_through:])
    return "".join(result)


def _candidate_replacement(
    value: str,
    start: int,
    *,
    managed_roots: tuple[str, ...],
    paths: _TaskPaths,
    path_style: _PathStyle,
) -> tuple[int, str, bool] | None:
    end = _candidate_end(value, start)
    if end <= start:
        return None
    candidate = _canonical_path(
        value[start:end],
        path_style=path_style,
    )
    display = _display_path_value(
        candidate,
        managed_roots=managed_roots,
        paths=paths,
        path_style=path_style,
    )
    if display is None:
        return None
    if (
        end < len(value)
        and value[end].isspace()
        and not _looks_like_file_path(candidate)
    ):
        end = _candidate_end_with_spaces(value, start)
        candidate = _canonical_path(
            value[start:end],
            path_style=path_style,
        )
        display = _display_path_value(
            candidate,
            managed_roots=managed_roots,
            paths=paths,
            path_style=path_style,
        )
        if display is None:
            return end, MANAGED_PATH_PLACEHOLDER, False
    reliable = (
        display != MANAGED_PATH_PLACEHOLDER
        or end == len(value)
        or value[end] in _CANDIDATE_TERMINATORS
        or _looks_like_file_path(candidate)
    )
    return end, display, reliable


def _candidate_end(value: str, start: int) -> int:
    end = start + 1
    while end < len(value):
        if value[end].isspace() or value[end] in _CANDIDATE_TERMINATORS:
            break
        end += 1
    return end


def _candidate_end_with_spaces(value: str, start: int) -> int:
    end = start + 1
    dot_in_component = False
    while end < len(value):
        character = value[end]
        if character in _CANDIDATE_TERMINATORS:
            break
        if character in "/\\":
            dot_in_component = False
        elif character == ".":
            dot_in_component = True
        elif character.isspace() and dot_in_component:
            break
        end += 1
    return end


def _display_exact_path(
    candidate: str,
    *,
    paths: _TaskPaths,
    path_style: _PathStyle,
) -> str | None:
    if not _is_absolute_path(candidate, path_style=path_style):
        return None
    for path in paths.inputs:
        if _paths_equal(candidate, path, path_style=path_style):
            return _basename(path)
    for path in paths.output_files:
        if _paths_equal(candidate, path, path_style=path_style):
            return _safe_output_name(path)
    for path in paths.output_roots:
        if _paths_equal(candidate, path, path_style=path_style):
            return _safe_output_name(path)
    return None


def _display_path_value(
    candidate: str,
    *,
    managed_roots: tuple[str, ...],
    paths: _TaskPaths,
    path_style: _PathStyle,
) -> str | None:
    exact = _display_exact_path(
        candidate,
        paths=paths,
        path_style=path_style,
    )
    if exact is not None:
        return exact
    if not _is_absolute_path(candidate, path_style=path_style):
        return None
    for path in paths.output_roots:
        relative = _relative_path(
            candidate,
            path,
            path_style=path_style,
        )
        if relative is None:
            continue
        if not relative:
            return _safe_output_name(path)
        parts = relative.split("/")
        if any(_OPAQUE_DIRECTORY_NAME.fullmatch(part) for part in parts):
            return MANAGED_PATH_PLACEHOLDER
        return "/".join(parts)
    if any(
        _relative_path(candidate, root, path_style=path_style) is not None
        for root in managed_roots
    ):
        return MANAGED_PATH_PLACEHOLDER
    return None


def _decode_path(value: str) -> str:
    return _decode_text(value).value


def _decode_text(value: str) -> _DecodedText:
    byte_values = bytearray()
    byte_starts: list[int] = []
    byte_ends: list[int] = []
    for source_index, character in enumerate(value):
        for byte_value in character.encode("utf-8", errors="replace"):
            byte_values.append(byte_value)
            byte_starts.append(source_index)
            byte_ends.append(source_index + 1)
            while (
                len(byte_values) >= 3
                and byte_values[-3] == ord("%")
                and _is_hex_byte(byte_values[-2])
                and _is_hex_byte(byte_values[-1])
            ):
                decoded_byte = (
                    _hex_byte_value(byte_values[-2]) * 16
                    + _hex_byte_value(byte_values[-1])
                )
                decoded_start = byte_starts[-3]
                decoded_end = byte_ends[-1]
                del byte_values[-3:]
                del byte_starts[-3:]
                del byte_ends[-3:]
                byte_values.append(decoded_byte)
                byte_starts.append(decoded_start)
                byte_ends.append(decoded_end)

    characters: list[str] = []
    source_spans: list[tuple[int, int]] = []
    byte_index = 0
    while byte_index < len(byte_values):
        width = _utf8_sequence_width(byte_values, byte_index)
        chunk = bytes(byte_values[byte_index : byte_index + width])
        try:
            character = chunk.decode("utf-8")
        except UnicodeDecodeError:
            character = "\ufffd"
            width = 1
        characters.append(character)
        source_spans.append(
            (
                byte_starts[byte_index],
                byte_ends[byte_index + width - 1],
            )
        )
        byte_index += width
    return _DecodedText("".join(characters), tuple(source_spans))


def _is_hex_byte(value: int) -> bool:
    return (
        ord("0") <= value <= ord("9")
        or ord("A") <= value <= ord("F")
        or ord("a") <= value <= ord("f")
    )


def _hex_byte_value(value: int) -> int:
    if value <= ord("9"):
        return value - ord("0")
    return (value | 0x20) - ord("a") + 10


def _utf8_sequence_width(values: bytearray, index: int) -> int:
    leading = values[index]
    if leading < 0x80:
        return 1
    if 0xC2 <= leading <= 0xDF:
        width = 2
    elif 0xE0 <= leading <= 0xEF:
        width = 3
    elif 0xF0 <= leading <= 0xF4:
        width = 4
    else:
        return 1
    if index + width > len(values):
        return 1
    if any(
        not 0x80 <= continuation <= 0xBF
        for continuation in values[index + 1 : index + width]
    ):
        return 1
    return width


def _canonical_path(
    value: str,
    *,
    path_style: _PathStyle | None = None,
) -> str:
    decoded = _decode_path(value)
    uri_candidate = decoded.replace("\\", "/")
    is_remote_file_uri = False
    if uri_candidate.casefold().startswith("file:"):
        parsed = urlsplit(uri_candidate)
        if parsed.netloc and parsed.netloc.casefold() != "localhost":
            decoded = f"//{parsed.netloc}{parsed.path}"
            is_remote_file_uri = True
        else:
            decoded = parsed.path
    normalized = decoded.replace("\\", "/")
    style = path_style or (
        "windows" if _is_windows_path(normalized) else "posix"
    )
    if is_remote_file_uri:
        style = "windows"
    if style == "windows" and re.match(r"^/[A-Za-z]:/", normalized):
        normalized = normalized[1:]
    if style == "windows":
        return ntpath.normpath(normalized).replace("\\", "/")
    if normalized.startswith("/"):
        normalized = re.sub(r"/+", "/", normalized)
        return posixpath.normpath(normalized)
    return normalized


def _paths_equal(
    left: str,
    right: str,
    *,
    path_style: _PathStyle,
) -> bool:
    if path_style == "windows":
        return left.casefold() == right.casefold()
    return left == right


def _relative_path(
    candidate: str,
    root: str,
    *,
    path_style: _PathStyle,
) -> str | None:
    normalized_root = root.rstrip("/")
    prefix = normalized_root + "/"
    comparison_candidate = candidate
    comparison_root = normalized_root
    comparison_prefix = prefix
    if path_style == "windows":
        comparison_candidate = candidate.casefold()
        comparison_root = normalized_root.casefold()
        comparison_prefix = prefix.casefold()
    if comparison_candidate == comparison_root:
        return ""
    if not comparison_candidate.startswith(comparison_prefix):
        return None
    return candidate[len(prefix) :]


def _is_absolute_path(path: str, *, path_style: _PathStyle) -> bool:
    if path_style == "windows":
        normalized = path.replace("\\", "/")
        return normalized.startswith("//") or bool(
            re.match(r"^[A-Za-z]:/", normalized)
        )
    return path.startswith("/")


def _is_windows_path(
    path: str,
    *,
    path_style: _PathStyle | None = None,
) -> bool:
    if path_style is not None:
        return path_style == "windows"
    normalized = path.replace("\\", "/")
    return path.startswith("\\\\") or bool(re.match(r"^[A-Za-z]:/", normalized))


def _path_style_for(
    path: Path | PurePosixPath | PureWindowsPath,
) -> _PathStyle:
    if isinstance(path, PureWindowsPath):
        return "windows"
    if isinstance(path, PurePosixPath):
        return "posix"
    raw_path = os.fspath(path)
    if raw_path.startswith("\\\\") or re.match(r"^[A-Za-z]:[\\/]", raw_path):
        return "windows"
    return "windows" if os.name == "nt" else "posix"


def _known_path_expression(path: str, *, path_style: _PathStyle) -> str:
    separator = (
        r"[\\/]+"
        r"(?:(?:\.[\\/]+)|(?:[^\\/\s?#]+[\\/]+\.\.[\\/]+))*"
    )
    if path_style == "windows" and re.match(r"^[A-Za-z]:/", path):
        drive, remainder = path[:2], path[3:]
        components = remainder.split("/") if remainder else []
        body = re.escape(drive)
        if components:
            body += separator + separator.join(map(re.escape, components))
        return rf"(?:file:[\\/]{{3}})?{body}"
    components = path.strip("/").split("/")
    body = separator.join(map(re.escape, components))
    if path_style == "windows":
        return rf"(?:file:)?[\\/]{{2,}}{body}"
    return rf"(?:file:[\\/]{{2}}(?:localhost)?)?[\\/]+{body}"


def _path_flags(path_style: _PathStyle) -> re.RegexFlag:
    return re.IGNORECASE if path_style == "windows" else re.NOFLAG


def _truncate_public_string(value: str) -> str:
    if len(value) <= MAX_PUBLIC_EVENT_STRING_LENGTH:
        return value
    prefix_length = (
        MAX_PUBLIC_EVENT_STRING_LENGTH - len(PUBLIC_EVENT_TRUNCATED_MARKER)
    )
    return value[:prefix_length] + PUBLIC_EVENT_TRUNCATED_MARKER


def _looks_like_file_path(path: str) -> bool:
    name = _basename(path)
    return "." in name and not name.endswith(".")


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def _safe_output_name(path: str) -> str:
    name = _basename(path)
    return "output" if _OPAQUE_DIRECTORY_NAME.fullmatch(name) else name
