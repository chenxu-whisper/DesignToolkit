from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

MANAGED_PATH_PLACEHOLDER = "[managed path]"
_INPUT_FIELDS = ("input_path", "input_paths")
_OUTPUT_FILE_FIELDS = ("output_path",)
_OUTPUT_ROOT_FIELDS = ("output_dir", "output_root")
_OPAQUE_DIRECTORY_NAME = re.compile(
    r"(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_PATH_TERMINATORS = r"""'"<>|,;)\]}"""


def sanitize_event_text(
    value: str,
    *,
    data_dir: Path,
    task_payload: Mapping[str, object] | None,
) -> str:
    managed_root = _canonical_path(str(data_dir))
    if not _is_absolute_path(managed_root):
        return value
    inputs, output_files, output_roots = _task_paths(
        task_payload,
        managed_root=managed_root,
    )
    direct_display = _display_path_value(
        value,
        managed_root=managed_root,
        inputs=inputs,
        output_files=output_files,
        output_roots=output_roots,
    )
    if direct_display is not None:
        return direct_display
    result = value
    for path in sorted(inputs, key=len, reverse=True):
        result = _replace_exact_path(result, path, _basename(path))
    for path in sorted(output_files, key=len, reverse=True):
        result = _replace_exact_path(result, path, _safe_output_name(path))
    for path in sorted(output_roots, key=len, reverse=True):
        result = _replace_output_root(result, path)
    return _replace_unmapped_managed_paths(result, managed_root)


def sanitize_event_details(
    value: dict[str, Any],
    *,
    data_dir: Path,
    task_payload: Mapping[str, object] | None,
) -> dict[str, Any]:
    sanitized = _sanitize_nested(
        value,
        data_dir=data_dir,
        task_payload=task_payload,
    )
    if not isinstance(sanitized, dict):
        raise TypeError("Event details must remain a dictionary")
    return sanitized


def _sanitize_nested(
    value: Any,
    *,
    data_dir: Path,
    task_payload: Mapping[str, object] | None,
) -> Any:
    if isinstance(value, str):
        return sanitize_event_text(
            value,
            data_dir=data_dir,
            task_payload=task_payload,
        )
    if isinstance(value, dict):
        return {
            _sanitize_nested(
                key,
                data_dir=data_dir,
                task_payload=task_payload,
            ): _sanitize_nested(
                item,
                data_dir=data_dir,
                task_payload=task_payload,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _sanitize_nested(
                item,
                data_dir=data_dir,
                task_payload=task_payload,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _sanitize_nested(
                item,
                data_dir=data_dir,
                task_payload=task_payload,
            )
            for item in value
        )
    return value


def _task_paths(
    payload: Mapping[str, object] | None,
    *,
    managed_root: str,
) -> tuple[set[str], set[str], set[str]]:
    if payload is None:
        return set(), set(), set()
    inputs = _field_paths(payload, _INPUT_FIELDS, managed_root=managed_root)
    output_files = _field_paths(
        payload,
        _OUTPUT_FILE_FIELDS,
        managed_root=managed_root,
    )
    output_roots = _field_paths(
        payload,
        _OUTPUT_ROOT_FIELDS,
        managed_root=managed_root,
    )
    return inputs, output_files, output_roots


def _field_paths(
    payload: Mapping[str, object],
    fields: tuple[str, ...],
    *,
    managed_root: str,
) -> set[str]:
    paths: set[str] = set()
    for field in fields:
        value = payload.get(field)
        values = value if isinstance(value, list) else [value]
        for candidate in values:
            if not isinstance(candidate, str):
                continue
            canonical = _canonical_path(candidate)
            if (
                _is_absolute_path(canonical)
                and _relative_path(canonical, managed_root) is not None
            ):
                paths.add(canonical)
    return paths


def _replace_exact_path(value: str, path: str, replacement: str) -> str:
    pattern = re.compile(
        _path_expression(path) + r"(?![\\/])",
        _path_flags(path),
    )
    return pattern.sub(lambda _match: replacement, value)


def _replace_output_root(value: str, path: str) -> str:
    expression = _path_expression(path)
    flags = _path_flags(path)
    result = re.sub(
        expression + r"[\\/]",
        "",
        value,
        flags=flags,
    )
    return re.sub(
        expression + r"(?![\\/])",
        lambda _match: _safe_output_name(path),
        result,
        flags=flags,
    )


def _replace_unmapped_managed_paths(value: str, managed_root: str) -> str:
    expression = _path_expression(managed_root)
    flags = _path_flags(managed_root)
    result = value
    for delimiter in ("'", '"'):
        result = re.sub(
            expression + rf"[^\r\n{delimiter}]*(?={delimiter})",
            MANAGED_PATH_PLACEHOLDER,
            result,
            flags=flags,
        )
    pattern = re.compile(
        expression
        + rf"(?:[\\/][^{_PATH_TERMINATORS}]+)*"
        + rf"(?=$|[{_PATH_TERMINATORS}])",
        flags,
    )
    return pattern.sub(MANAGED_PATH_PLACEHOLDER, result)


def _display_path_value(
    value: str,
    *,
    managed_root: str,
    inputs: set[str],
    output_files: set[str],
    output_roots: set[str],
) -> str | None:
    candidate = _canonical_path(value)
    if not _is_absolute_path(candidate):
        return None
    for path in inputs:
        if _paths_equal(candidate, path):
            return _basename(path)
    for path in output_files:
        if _paths_equal(candidate, path):
            return _safe_output_name(path)
    for path in output_roots:
        relative = _relative_path(candidate, path)
        if relative is None:
            continue
        if not relative:
            return _safe_output_name(path)
        parts = relative.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            return MANAGED_PATH_PLACEHOLDER
        return "/".join(parts)
    if _relative_path(candidate, managed_root) is not None:
        return MANAGED_PATH_PLACEHOLDER
    return None


def _paths_equal(left: str, right: str) -> bool:
    if _is_windows_drive_path(left) and _is_windows_drive_path(right):
        return left.casefold() == right.casefold()
    return left == right


def _relative_path(candidate: str, root: str) -> str | None:
    normalized_root = root.rstrip("/")
    prefix = normalized_root + "/"
    comparison_candidate = candidate
    comparison_root = normalized_root
    comparison_prefix = prefix
    if _is_windows_drive_path(candidate) and _is_windows_drive_path(root):
        comparison_candidate = candidate.casefold()
        comparison_root = normalized_root.casefold()
        comparison_prefix = prefix.casefold()
    if comparison_candidate == comparison_root:
        return ""
    if not comparison_candidate.startswith(comparison_prefix):
        return None
    return candidate[len(prefix) :]


def _path_expression(path: str) -> str:
    encoded = quote(path, safe="/:")
    raw_patterns = {
        _literal_path_expression(path),
        _literal_path_expression(encoded),
    }
    if _is_windows_drive_path(path):
        uri_prefix = r"file:[\\/]{3}"
    else:
        uri_prefix = r"file:[\\/]{2}"
    uri_patterns = {uri_prefix + pattern for pattern in raw_patterns}
    return "(?:" + "|".join(sorted(raw_patterns | uri_patterns)) + ")"


def _literal_path_expression(path: str) -> str:
    return "".join(
        r"[\\/]" if character in "/\\" else re.escape(character) for character in path
    )


def _canonical_path(value: str) -> str:
    decoded = unquote(value)
    if decoded.lower().startswith("file:"):
        parsed = urlsplit(decoded)
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            decoded = f"//{parsed.netloc}{parsed.path}"
        else:
            decoded = parsed.path
        if re.match(r"^/[A-Za-z]:/", decoded):
            decoded = decoded[1:]
    normalized = decoded.replace("\\", "/")
    if normalized.startswith("//"):
        prefix = "//"
        normalized = prefix + re.sub(r"/+", "/", normalized[2:])
    else:
        normalized = re.sub(r"/+", "/", normalized)
    return normalized


def _is_absolute_path(path: str) -> bool:
    return (
        path.startswith("/")
        or path.startswith("//")
        or bool(re.match(r"^[A-Za-z]:/", path))
    )


def _is_windows_drive_path(path: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:/", path))


def _path_flags(path: str) -> re.RegexFlag:
    return re.IGNORECASE if _is_windows_drive_path(path) else re.NOFLAG


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def _safe_output_name(path: str) -> str:
    name = _basename(path)
    return "output" if _OPAQUE_DIRECTORY_NAME.fullmatch(name) else name
