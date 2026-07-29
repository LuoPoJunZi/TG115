from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main() -> int:
    data_dir = Path(os.getenv("DATA_DIR", "/data"))
    heartbeat = data_dir / "heartbeat"
    if not heartbeat.exists():
        return 1
    try:
        age = time.time() - heartbeat.stat().st_mtime
    except OSError:
        return 1
    return 0 if age < 90 else 1


if __name__ == "__main__":
    sys.exit(main())
