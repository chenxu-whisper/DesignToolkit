from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path


def default_local_path_roots() -> list[Path]:
    project_root = Path.cwd()
    candidate_roots = [project_root, *_main_project_roots_from_worktree(project_root)]
    candidates = []
    for root in candidate_roots:
        candidates.extend(
            [
                root / "assets" / "video",
                root / "assets",
                root,
            ],
        )
    return _unique_existing_directories(candidates)


def resolve_dropped_file_paths(
    names: Sequence[str],
    *,
    roots: Sequence[Path] | None = None,
) -> tuple[list[Path], list[str]]:
    search_roots = list(roots) if roots is not None else default_local_path_roots()
    resolved: list[Path] = []
    unresolved: list[str] = []

    for raw_name in names:
        name = Path(raw_name).name
        match = _find_first_file_by_name(search_roots, name)
        if match is None:
            unresolved.append(raw_name)
        else:
            resolved.append(match)

    return resolved, unresolved


def _find_first_file_by_name(roots: Sequence[Path], name: str) -> Path | None:
    for root in _unique_existing_directories(roots):
        candidate = root / name
        if candidate.is_file():
            return candidate
        for path in root.rglob(name):
            if path.is_file():
                return path
    return None


def _unique_existing_directories(paths: Iterable[Path]) -> list[Path]:
    directories: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen or not resolved.is_dir():
            continue
        directories.append(resolved)
        seen.add(resolved)
    return directories


def _main_project_roots_from_worktree(path: Path) -> list[Path]:
    parts = path.resolve().parts
    if ".worktrees" not in parts:
        return []
    worktrees_index = parts.index(".worktrees")
    if worktrees_index == 0:
        return []
    return [Path(*parts[:worktrees_index])]
