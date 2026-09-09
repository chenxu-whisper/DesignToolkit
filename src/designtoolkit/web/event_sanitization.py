from __future__ import annotations

import codecs
import ntpath
import os
import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, time, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic_core import to_jsonable_python

from designtoolkit.web.schemas import LogEventResponse

MANAGED_PATH_PLACEHOLDER = "[managed path]"
PUBLIC_EVENT_TRUNCATED_MARKER = " [truncated]"
UNSUPPORTED_PUBLIC_EVENT_VALUE = "[unsupported value]"
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
_PATH_BOUNDARY_PUNCTUATION = frozenset(
    "!\"#$%&'()*+,-.:;<=>?@[\\]^`{|}~"
)
_MAX_PUBLIC_EVENT_BYTES = MAX_PUBLIC_EVENT_STRING_LENGTH * 4 + 4
_PathStyle = Literal["posix", "windows"]


@dataclass(frozen=True)
class _TaskPaths:
    replacements: Mapping[str, str]
    output_roots: Mapping[str, str]


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
                "message": self._sanitize_message(
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
        bounded_source = value[:MAX_PUBLIC_EVENT_STRING_LENGTH]
        was_truncated = len(value) > MAX_PUBLIC_EVENT_STRING_LENGTH
        value = _truncate_public_string(value)
        if not self._managed_roots:
            return value
        if not was_truncated and not any(
            character in value for character in "\r\n?#"
        ):
            exact_candidate = _canonical_path(
                value,
                path_style=self._path_style,
            )
            exact = _display_path_value(
                exact_candidate,
                managed_roots=self._managed_roots,
                paths=paths,
                path_style=self._path_style,
            )
            if exact is not None:
                return exact
        decoded = _decode_text(value)
        if decoded.value != value:
            direct_root_starts = _managed_candidate_starts(
                decoded.value,
                managed_roots=self._managed_roots,
                path_style=self._path_style,
            )
            if direct_root_starts or _contains_managed_root(
                decoded.value,
                managed_roots=self._managed_roots,
                path_style=self._path_style,
            ):
                return _managed_path_placeholder(was_truncated=was_truncated)
        candidate_replacements = _path_candidate_replacements(
            decoded.value,
            managed_roots=self._managed_roots,
            paths=paths,
            path_style=self._path_style,
        )
        if candidate_replacements is None:
            return _managed_path_placeholder(was_truncated=was_truncated)
        candidate = _apply_text_replacements(
            value,
            decoded=decoded,
            replacements=candidate_replacements,
        )
        residual_source = bounded_source if was_truncated else candidate
        if _contains_managed_root(
            residual_source,
            managed_roots=self._managed_roots,
            path_style=self._path_style,
        ):
            return _managed_path_placeholder(was_truncated=was_truncated)
        return candidate

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
            active_containers=set(),
        )
        if not isinstance(sanitized, dict):
            raise TypeError("Event details must remain a dictionary")
        return sanitized

    def _sanitize_message(self, value: object, *, paths: _TaskPaths) -> str:
        binary = _event_bytes(value)
        if binary is not None:
            return self._sanitize_bytes(binary, paths=paths)
        text = _event_text(value)
        if text is None:
            return UNSUPPORTED_PUBLIC_EVENT_VALUE
        return self._sanitize_text(text, paths=paths)

    def _sanitize_bytes(self, value: bytes, *, paths: _TaskPaths) -> str:
        bounded = value[:_MAX_PUBLIC_EVENT_BYTES]
        utf8 = _decode_bytes(bounded, "utf-8")
        cleaned_utf8 = (
            utf8.removeprefix("\ufeff") if utf8 is not None else None
        )
        utf8_has_managed_root = (
            cleaned_utf8 is not None
            and _contains_managed_root(
                cleaned_utf8,
                managed_roots=self._managed_roots,
                path_style=self._path_style,
            )
        )
        requires_wide_scan = (
            cleaned_utf8 is None
            or "\x00" in cleaned_utf8
            or cleaned_utf8 != utf8
            or not utf8_has_managed_root
        )
        if utf8_has_managed_root and not requires_wide_scan:
            return self._sanitize_text(cleaned_utf8, paths=paths)

        decoded_candidates = (
            _wide_text_candidates(bounded) if requires_wide_scan else ()
        )
        for candidate in decoded_candidates:
            if _contains_managed_root(
                candidate,
                managed_roots=self._managed_roots,
                path_style=self._path_style,
            ):
                return MANAGED_PATH_PLACEHOLDER

        if utf8_has_managed_root and cleaned_utf8 is not None:
            return self._sanitize_text(cleaned_utf8, paths=paths)
        if cleaned_utf8 is not None and "\x00" not in cleaned_utf8:
            return self._sanitize_text(cleaned_utf8, paths=paths)
        if decoded_candidates:
            return self._sanitize_text(decoded_candidates[0], paths=paths)
        return UNSUPPORTED_PUBLIC_EVENT_VALUE

    def _sanitize_nested(
        self,
        value: Any,
        *,
        paths: _TaskPaths,
        budget: _TraversalBudget,
        depth: int,
        active_containers: set[int],
    ) -> Any:
        if not budget.consume():
            return PUBLIC_EVENT_TRUNCATED_MARKER
        if isinstance(value, Enum):
            return self._sanitize_json_scalar(value, paths=paths)
        if isinstance(value, str):
            return self._sanitize_text(
                value,
                paths=paths,
            )
        binary = _event_bytes(value)
        if binary is not None:
            return self._sanitize_bytes(binary, paths=paths)
        text = _event_text(value)
        if text is not None:
            return self._sanitize_text(text, paths=paths)
        if value is None or isinstance(value, (bool, int, float)):
            return to_jsonable_python(value, inf_nan_mode="null")
        if isinstance(value, (date, time, timedelta, Decimal, UUID, complex)):
            return self._sanitize_json_scalar(value, paths=paths)
        is_container = isinstance(
            value,
            (dict, list, tuple, set, frozenset),
        )
        if depth >= MAX_PUBLIC_EVENT_DEPTH and is_container:
            return PUBLIC_EVENT_TRUNCATED_MARKER
        if not is_container:
            return UNSUPPORTED_PUBLIC_EVENT_VALUE
        container_id = id(value)
        if container_id in active_containers:
            return PUBLIC_EVENT_TRUNCATED_MARKER
        active_containers.add(container_id)
        try:
            return self._sanitize_container(
                value,
                paths=paths,
                budget=budget,
                depth=depth,
                active_containers=active_containers,
            )
        finally:
            active_containers.remove(container_id)

    def _sanitize_json_scalar(
        self,
        value: object,
        *,
        paths: _TaskPaths,
    ) -> Any:
        try:
            normalized = to_jsonable_python(value, inf_nan_mode="null")
        except (TypeError, ValueError):
            return UNSUPPORTED_PUBLIC_EVENT_VALUE
        if isinstance(normalized, str):
            return self._sanitize_text(normalized, paths=paths)
        if normalized is None or isinstance(normalized, (bool, int, float)):
            return normalized
        return UNSUPPORTED_PUBLIC_EVENT_VALUE

    def _sanitize_container(
        self,
        value: dict[Any, Any] | list[Any] | tuple[Any, ...] | set[Any] | frozenset[Any],
        *,
        paths: _TaskPaths,
        budget: _TraversalBudget,
        depth: int,
        active_containers: set[int],
    ) -> dict[str, Any] | list[Any]:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                if budget.remaining_nodes <= 0:
                    result[PUBLIC_EVENT_TRUNCATED_MARKER] = (
                        PUBLIC_EVENT_TRUNCATED_MARKER
                    )
                    break
                sanitized_key = self._sanitize_mapping_key(
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
                    active_containers=active_containers,
                )
                result[sanitized_key] = sanitized_item
            return result
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
                    active_containers=active_containers,
                )
            )
        return result_list

    def _sanitize_mapping_key(
        self,
        value: object,
        *,
        paths: _TaskPaths,
        budget: _TraversalBudget,
        depth: int,
    ) -> str:
        if not budget.consume():
            return PUBLIC_EVENT_TRUNCATED_MARKER
        binary = _event_bytes(value)
        if binary is not None:
            return self._sanitize_bytes(binary, paths=paths)
        text = _json_key_text(
            value,
            budget=budget,
            depth=depth,
        )
        return self._sanitize_text(text, paths=paths)

    def _task_paths(
        self,
        task_payload: Mapping[str, object] | None,
    ) -> _TaskPaths:
        return _task_paths(
            task_payload,
            managed_roots=self._managed_roots,
            path_style=self._path_style,
        )


def _event_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, os.PathLike):
        try:
            raw_path = os.fspath(value)
        except Exception:
            return None
        if isinstance(raw_path, str):
            return raw_path
    return None


def _event_bytes(value: object) -> bytes | None:
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, os.PathLike):
        try:
            raw_path = os.fspath(value)
        except Exception:
            return None
        if isinstance(raw_path, bytes):
            return raw_path
    return None


def _json_key_text(
    value: object,
    *,
    budget: _TraversalBudget,
    depth: int,
) -> str:
    text = _event_text(value)
    if text is not None:
        return text
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, tuple):
        if depth >= MAX_PUBLIC_EVENT_DEPTH:
            return PUBLIC_EVENT_TRUNCATED_MARKER
        parts: list[str] = []
        for item in value:
            if not budget.consume():
                parts.append(PUBLIC_EVENT_TRUNCATED_MARKER)
                break
            parts.append(
                _json_key_text(
                    item,
                    budget=budget,
                    depth=depth + 1,
                )
            )
        return ",".join(parts)
    if isinstance(value, (int, float, date, time, timedelta, Decimal, Enum, UUID)):
        try:
            normalized = to_jsonable_python(
                {value: None},
                inf_nan_mode="null",
            )
        except (TypeError, ValueError):
            return UNSUPPORTED_PUBLIC_EVENT_VALUE
        return next(iter(normalized))
    return UNSUPPORTED_PUBLIC_EVENT_VALUE


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
        return _TaskPaths({}, {})
    replacements: dict[str, str] = {}
    output_roots: dict[str, str] = {}
    field_groups = (
        (_INPUT_FIELDS, False, _basename),
        (_OUTPUT_FILE_FIELDS, False, _safe_output_name),
        (_OUTPUT_ROOT_FIELDS, True, _safe_output_name),
    )
    for fields, is_output_root, display_name in field_groups:
        characters = 0
        candidate_count = 0
        exhausted = False
        for field in fields:
            value = payload.get(field)
            values = value if isinstance(value, list) else [value]
            for candidate in values:
                if candidate_count >= _MAX_TASK_PATH_CANDIDATES:
                    exhausted = True
                    break
                candidate_count += 1
                if not isinstance(candidate, str):
                    continue
                remaining = _MAX_TASK_PATH_CHARACTERS - characters
                if remaining <= 0:
                    exhausted = True
                    break
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
                display = display_name(canonical)
                for alias in aliases:
                    key = _canonical_path_key(alias, path_style=path_style)
                    replacements[key] = display
                    if is_output_root:
                        output_roots[key] = alias
            if exhausted:
                break
    return _TaskPaths(replacements, output_roots)


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


def _path_candidate_replacements(
    value: str,
    *,
    managed_roots: tuple[str, ...],
    paths: _TaskPaths,
    path_style: _PathStyle,
) -> tuple[_TextReplacement, ...] | None:
    result: list[_TextReplacement] = []
    candidate_starts = _managed_candidate_starts(
        value,
        managed_roots=managed_roots,
        path_style=path_style,
    )
    candidate_index = 0
    while candidate_index < len(candidate_starts):
        index = candidate_starts[candidate_index]
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
        result.append(_TextReplacement(start=index, end=end, value=display))
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
            expression,
            value,
            flags=_path_flags(path_style),
        ):
            if _is_managed_root_boundary(value, match.start(), match.end()):
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
    return paths.replacements.get(
        _canonical_path_key(candidate, path_style=path_style)
    )


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
    ancestor = candidate.rstrip("/")
    while "/" in ancestor:
        ancestor = ancestor.rsplit("/", 1)[0]
        root = paths.output_roots.get(
            _canonical_path_key(ancestor, path_style=path_style)
        )
        if root is None:
            continue
        relative = candidate[len(root.rstrip("/")) :].lstrip("/")
        if not relative:
            return _safe_output_name(root)
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


def _contains_managed_root(
    value: str,
    *,
    managed_roots: tuple[str, ...],
    path_style: _PathStyle,
) -> bool:
    shadows = [_normalized_path_shadow(value, path_style=path_style)]
    if "+" in value:
        shadows.append(
            _normalized_path_shadow(
                value,
                path_style=path_style,
                plus_as_space=True,
            )
        )
    for root in managed_roots:
        root_shadow = _normalized_path_shadow(root, path_style=path_style)
        if any(_shadow_contains_root(shadow, root_shadow) for shadow in shadows):
            return True
    return False


def _normalized_path_shadow(
    value: str,
    *,
    path_style: _PathStyle,
    plus_as_space: bool = False,
) -> str:
    decoded = _decode_text(value).value
    if plus_as_space:
        decoded = decoded.replace("+", " ")
    decoded = decoded.replace("\\", "/")
    absolute = decoded.startswith("/")
    components: list[str] = []
    for component in decoded.split("/"):
        if not component or component == ".":
            continue
        if component == "..":
            if components and components[-1] != "..":
                components.pop()
            elif not absolute:
                components.append(component)
            continue
        components.append(component)
    shadow = "/".join(components)
    if absolute:
        shadow = "/" + shadow
    return shadow.casefold() if path_style == "windows" else shadow


def _decode_bytes(value: bytes, encoding: str) -> str | None:
    try:
        return value.decode(encoding)
    except (UnicodeDecodeError, ValueError):
        return None


def _wide_text_candidates(value: bytes) -> tuple[str, ...]:
    encodings = ["utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"]
    bom_encoding: str | None = None
    for bom, encoding in (
        (codecs.BOM_UTF32_LE, "utf-32-le"),
        (codecs.BOM_UTF32_BE, "utf-32-be"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if value.startswith(bom):
            bom_encoding = encoding
            encodings.remove(encoding)
            encodings.insert(0, encoding)
            break

    candidates: dict[str, tuple[int, int, int]] = {}
    for encoding in encodings:
        width = 4 if encoding.startswith("utf-32") else 2
        if len(value) % width:
            continue
        decoded = _decode_bytes(value, encoding)
        if decoded is None:
            continue
        decoded = decoded.removeprefix("\ufeff")
        if not _is_reasonable_text(decoded):
            continue
        ascii_characters = sum(
            character.isascii() and character.isprintable()
            for character in decoded
        )
        printable_characters = sum(
            character.isprintable() or character in "\r\n\t"
            for character in decoded
        )
        candidates[decoded] = (
            int(encoding == bom_encoding),
            ascii_characters,
            printable_characters,
        )
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: candidates[candidate],
            reverse=True,
        )
    )


def _is_reasonable_text(value: str) -> bool:
    if not value or "\x00" in value or "\ufffd" in value:
        return False
    return all(
        character.isprintable() or character in "\r\n\t"
        for character in value
    )


def _shadow_contains_root(shadow: str, root: str) -> bool:
    start = 0
    while True:
        index = shadow.find(root, start)
        if index < 0:
            return False
        if _is_managed_root_boundary(shadow, index, index + len(root)):
            return True
        start = index + 1


def _is_managed_root_boundary(value: str, start: int, end: int) -> bool:
    if start > 0:
        previous = value[start - 1]
        if (
            not previous.isspace()
            and previous not in "/\\"
            and previous not in _PATH_BOUNDARY_PUNCTUATION
        ):
            return False
    if end >= len(value):
        return True
    following = value[end]
    return (
        following.isspace()
        or following in "/\\?#"
        or following in _PATH_BOUNDARY_PUNCTUATION
    )


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


def _canonical_path_key(value: str, *, path_style: _PathStyle) -> str:
    return value.casefold() if path_style == "windows" else value


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


def _managed_path_placeholder(*, was_truncated: bool) -> str:
    if was_truncated:
        return MANAGED_PATH_PLACEHOLDER + PUBLIC_EVENT_TRUNCATED_MARKER
    return MANAGED_PATH_PLACEHOLDER


def _looks_like_file_path(path: str) -> bool:
    name = _basename(path)
    return "." in name and not name.endswith(".")


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def _safe_output_name(path: str) -> str:
    name = _basename(path)
    return "output" if _OPAQUE_DIRECTORY_NAME.fullmatch(name) else name
