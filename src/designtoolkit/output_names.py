from __future__ import annotations

from hashlib import sha256

PORTABLE_NAME_MAX_BYTES = 255


def bounded_derived_name(
    *,
    prefix: str,
    stem: str,
    suffix: str,
    extension: str = "",
    max_bytes: int = PORTABLE_NAME_MAX_BYTES,
) -> str:
    candidate = f"{prefix}{stem}{suffix}{extension}"
    if len(candidate.encode("utf-8")) <= max_bytes:
        return candidate

    digest = sha256(stem.encode("utf-8")).hexdigest()[:12]
    fixed = f"{prefix}_{digest}{suffix}{extension}"
    available = max_bytes - len(fixed.encode("utf-8"))
    if available < 1:
        raise ValueError("Derived output filename has no room for its input stem")
    truncated = stem.encode("utf-8")[:available].decode("utf-8", errors="ignore")
    return f"{prefix}{truncated}_{digest}{suffix}{extension}"
