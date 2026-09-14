from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import psutil


def healthy(data_dir: Path, *, now: float, started_at: float = 0) -> bool:
    try:
        for name in ("heartbeat", "resource-heartbeat"):
            modified = (data_dir / name).stat().st_mtime
            if modified < started_at or not 0 <= now - modified < 90:
                return False
    except OSError:
        return False
    return True


def main() -> int:
    data_dir = Path(os.getenv("DATA_DIR", "/data"))
    try:
        started = psutil.Process(1).create_time() if sys.platform == "linux" else 0
        return 0 if healthy(data_dir, now=time.time(), started_at=started) else 1
    except psutil.Error:
        return 1


if __name__ == "__main__":
    sys.exit(main())
