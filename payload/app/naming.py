from __future__ import annotations

import re
from pathlib import Path


def truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def safe_file_name(name: str, fallback: str) -> str:
    candidate = Path(name or fallback).name
    candidate = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", candidate)
    candidate = candidate.strip(" .")
    if not candidate:
        candidate = fallback
    suffix = truncate_utf8(Path(candidate).suffix, 24)
    stem_budget = max(1, 180 - len(suffix.encode("utf-8")))
    stem = truncate_utf8(Path(candidate).stem, stem_budget)
    if not stem:
        stem = "file"
    return f"{stem}{suffix}"
