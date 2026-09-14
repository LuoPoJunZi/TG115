from __future__ import annotations

import argparse
import os
import sqlite3
from contextlib import closing
from pathlib import Path


def backup_database(source: Path, destination: Path) -> None:
    """Create and integrity-check a consistent SQLite online backup."""
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise FileNotFoundError("源数据库不存在")
    if source == destination:
        raise ValueError("备份目标不能与源数据库相同")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    try:
        with (
            closing(sqlite3.connect(source)) as original,
            closing(sqlite3.connect(temporary)) as copied,
        ):
            original.backup(copied)
            result = copied.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError("数据库备份完整性检查失败")
        os.chmod(temporary, 0o600)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="创建 TG115 SQLite 一致性备份")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        backup_database(args.source, args.destination)
    except Exception:  # noqa: BLE001 - CLI boundary must not reveal paths or row data
        print("TG115_DATABASE_BACKUP=FAILED")
        return 1
    print("TG115_DATABASE_BACKUP=OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
