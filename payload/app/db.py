from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .naming import safe_file_name
from .states import GROWING_STATES, LOCAL_STATES, STATE_LABELS, can_transition

SCHEMA_VERSION = 1


class TaskDB:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path, timeout=30, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=30000")
            if self._conn.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
                self._conn.close()
                raise RuntimeError("数据库版本高于当前程序；请恢复匹配的程序，不能降级打开")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    sender_id INTEGER NOT NULL,
                    media_key TEXT,
                    file_name TEXT NOT NULL,
                    file_size INTEGER NOT NULL CHECK(file_size >= 0),
                    transfer_mode TEXT NOT NULL DEFAULT 'local',
                    state TEXT NOT NULL,
                    local_path TEXT,
                    remote_path TEXT,
                    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                    uploaded_bytes INTEGER NOT NULL DEFAULT 0,
                    download_retries INTEGER NOT NULL DEFAULT 0,
                    upload_retries INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    wait_reason TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    next_retry_at REAL NOT NULL DEFAULT 0,
                    UNIQUE(chat_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_state_created
                    ON tasks(state, created_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_retry
                    ON tasks(state, next_retry_at);
                """
            )
            columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "media_key" not in columns:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN media_key TEXT")
            if "transfer_mode" not in columns:
                self._conn.execute(
                    "ALTER TABLE tasks ADD COLUMN transfer_mode TEXT "
                    "NOT NULL DEFAULT 'local'"
                )
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_sender_media
                ON tasks(sender_id, media_key)
                WHERE media_key IS NOT NULL
                """
            )
            self._migrate(columns)

    def _migrate(self, columns: set[str]) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for name, declaration in (
                ("remote_temp_path", "TEXT"),
                ("remote_final_path", "TEXT"),
                ("cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")
            self._conn.execute(
                """UPDATE tasks SET remote_temp_path=remote_path
                WHERE remote_temp_path IS NULL AND remote_path LIKE '.uploading-%'"""
            )
            self._conn.execute(
                """UPDATE tasks SET remote_final_path=remote_path
                WHERE remote_final_path IS NULL AND remote_path IS NOT NULL
                AND remote_path NOT LIKE '.uploading-%'"""
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS service_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
                old_state TEXT, new_state TEXT NOT NULL, happened_at REAL NOT NULL)"""
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id, id)"
            )
            self._conn.execute(
                """CREATE TRIGGER IF NOT EXISTS task_state_event AFTER UPDATE OF state ON tasks
                WHEN OLD.state != NEW.state BEGIN
                    INSERT INTO task_events(task_id,old_state,new_state,happened_at)
                    VALUES(NEW.id,OLD.state,NEW.state,NEW.updated_at);
                END"""
            )
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def is_paused(self) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM service_meta WHERE key='paused'"
            ).fetchone()
            return bool(row and row[0] == "1")

    def remote_claimed(self, path: str, task_id: int) -> bool:
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM tasks WHERE remote_final_path=? AND id!=? "
                "AND state!='cancelled' LIMIT 1", (path, task_id)
            ).fetchone() is not None

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO service_meta(key,value) VALUES('paused',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if paused else "0",),
            )

    def events(self, task_id: int, limit: int = 8) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(
                "SELECT old_state,new_state,happened_at FROM task_events "
                "WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit)
            ).fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def create_task(
        self,
        *,
        chat_id: int,
        message_id: int,
        sender_id: int,
        media_key: str | None = None,
        file_name: str,
        file_size: int,
    ) -> tuple[dict[str, Any], bool]:
        now = time.time()
        with self._lock:
            try:
                cursor = self._conn.execute(
                    """
                    INSERT INTO tasks (
                        chat_id, message_id, sender_id, media_key, file_name,
                        file_size, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (
                        chat_id,
                        message_id,
                        sender_id,
                        media_key,
                        file_name,
                        file_size,
                        now,
                        now,
                    ),
                )
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
                return dict(row), True
            except sqlite3.IntegrityError:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE chat_id = ? AND message_id = ?",
                    (chat_id, message_id),
                ).fetchone()
                if row is None and media_key:
                    row = self._conn.execute(
                        """
                        SELECT * FROM tasks
                        WHERE sender_id = ? AND media_key = ?
                        """,
                        (sender_id, media_key),
                    ).fetchone()
                if row is None:
                    raise
                return dict(row), False

    def get(self, task_id: int) -> dict[str, Any] | None:
        with self._lock:
            return self._dict(
                self._conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
            )

    def list_states(
        self,
        states: Iterable[str],
        *,
        limit: int = 200,
        ready_only: bool = False,
        after_id: int = 0,
    ) -> list[dict[str, Any]]:
        state_values = tuple(states)
        if not state_values:
            return []
        placeholders = ",".join("?" for _ in state_values)
        # placeholders are generated only from the tuple length; values stay bound.
        query = f"SELECT * FROM tasks WHERE state IN ({placeholders})"  # nosec B608
        params: list[Any] = list(state_values)
        if after_id > 0:
            query += " AND id > ?"
            params.append(after_id)
        if ready_only:
            query += " AND next_retry_at <= ? AND cancel_requested=0"
            params.append(time.time())
        query += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            return [
                dict(row)
                for row in self._conn.execute(query, params).fetchall()
            ]

    def list_recent(self, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        if limit <= 0 or offset < 0:
            raise ValueError("limit 必须大于 0，offset 不能小于 0")
        with self._lock:
            return [
                dict(row)
                for row in self._conn.execute(
                    "SELECT * FROM tasks ORDER BY id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            ]

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                row["state"]: row["count"]
                for row in self._conn.execute(
                    "SELECT state, COUNT(*) AS count FROM tasks GROUP BY state"
                ).fetchall()
            }

    def used_local_bytes(self) -> int:
        placeholders = ",".join("?" for _ in LOCAL_STATES)
        local_sum_query = (
            "SELECT COALESCE(SUM(file_size), 0) AS total "  # nosec B608
            f"FROM tasks WHERE state IN ({placeholders}) "
            "AND transfer_mode != 'stream'"
        )
        with self._lock:
            row = self._conn.execute(
                local_sum_query,
                LOCAL_STATES,
            ).fetchone()
            return int(row["total"])

    def reserve(
        self,
        task_id: int,
        *,
        budget_bytes: int,
        current_free_bytes: int,
        minimum_free_bytes: int,
    ) -> tuple[bool, str]:
        placeholders = ",".join("?" for _ in LOCAL_STATES)
        local_sum_query = (
            "SELECT COALESCE(SUM(file_size), 0) AS total "  # nosec B608
            f"FROM tasks WHERE state IN ({placeholders}) "
            "AND transfer_mode != 'stream'"
        )
        growth_placeholders = ",".join("?" for _ in GROWING_STATES)
        growth_query = (
            "SELECT COALESCE(SUM(CASE "  # nosec B608
            "WHEN file_size > downloaded_bytes "
            "THEN file_size - downloaded_bytes ELSE 0 END), 0) AS total "
            f"FROM tasks WHERE state IN ({growth_placeholders}) "
            "AND transfer_mode != 'stream'"
        )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                task = self._conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if task is None or task["state"] != "queued" or task["cancel_requested"]:
                    self._conn.execute("ROLLBACK")
                    return False, "任务状态已经改变"
                row = self._conn.execute(
                    local_sum_query,
                    LOCAL_STATES,
                ).fetchone()
                used = int(row["total"])
                growth_row = self._conn.execute(
                    growth_query,
                    GROWING_STATES,
                ).fetchone()
                pending_growth = int(growth_row["total"])
                size = int(task["file_size"])
                requested_stream = task["transfer_mode"] == "stream"
                if requested_stream or size > budget_bytes:
                    if current_free_bytes <= minimum_free_bytes:
                        reason = "等待真实磁盘空间恢复后使用流式模式"
                        self._conn.execute(
                            """
                            UPDATE tasks SET wait_reason = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (reason, time.time(), task_id),
                        )
                        self._conn.execute("COMMIT")
                        return False, reason
                    self._conn.execute(
                        """
                        UPDATE tasks
                        SET state = 'reserved', transfer_mode = 'stream',
                            wait_reason = NULL, error = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (time.time(), task_id),
                    )
                    self._conn.execute("COMMIT")
                    return True, (
                        "已按用户选择使用流式模式"
                        if requested_stream
                        else "单文件超过本地预算，已切换流式模式"
                    )
                if used + size > budget_bytes:
                    reason = "等待本地临时空间额度"
                    self._conn.execute(
                        """
                        UPDATE tasks SET wait_reason = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (reason, time.time(), task_id),
                    )
                    self._conn.execute("COMMIT")
                    return False, reason
                if (
                    current_free_bytes - pending_growth - size
                    < minimum_free_bytes
                ):
                    shortfall = minimum_free_bytes + pending_growth + size - current_free_bytes
                    reason = (
                        "等待真实磁盘空间（至少还需释放 "
                        f"{shortfall / 1024**3:.2f}GB；可用 /stream #{task_id} 改为流式）"
                    )
                    self._conn.execute(
                        """
                        UPDATE tasks SET wait_reason = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (reason, time.time(), task_id),
                    )
                    self._conn.execute("COMMIT")
                    return False, reason
                self._conn.execute(
                    """
                    UPDATE tasks
                    SET state = 'reserved', transfer_mode = 'local',
                        wait_reason = NULL, error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (time.time(), task_id),
                )
                self._conn.execute("COMMIT")
                return True, ""
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def update(self, task_id: int, **fields: Any) -> None:
        if "state" in fields and fields["state"] not in STATE_LABELS:
            raise ValueError("未知任务状态")
        if "transfer_mode" in fields and fields["transfer_mode"] not in {"local", "stream"}:
            raise ValueError("未知传输模式")
        allowed = {
            "state",
            "transfer_mode",
            "local_path",
            "remote_path",
            "remote_temp_path",
            "remote_final_path",
            "cancel_requested",
            "downloaded_bytes",
            "uploaded_bytes",
            "download_retries",
            "upload_retries",
            "error",
            "wait_reason",
            "next_retry_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"不允许更新字段：{sorted(unknown)}")
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [task_id]
        with self._lock:
            self._conn.execute(
                # assignments contains names from the fixed allowed set above.
                f"UPDATE tasks SET {assignments} WHERE id = ?",  # nosec B608
                values,
            )

    def transition(self, task_id: int, new_state: str, **fields: Any) -> None:
        """Atomically validate and persist one legal state transition."""
        if new_state not in STATE_LABELS:
            raise ValueError("未知任务状态")
        if "state" in fields:
            raise ValueError("状态必须通过 new_state 参数指定")
        allowed = {
            "transfer_mode", "local_path", "remote_path", "remote_temp_path",
            "remote_final_path", "cancel_requested", "downloaded_bytes",
            "uploaded_bytes", "download_retries", "upload_retries", "error",
            "wait_reason", "next_retry_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"不允许更新字段：{sorted(unknown)}")
        if "transfer_mode" in fields and fields["transfer_mode"] not in {"local", "stream"}:
            raise ValueError("未知传输模式")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT state FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"任务 #{task_id} 不存在")
                old_state = str(row["state"])
                if not can_transition(old_state, new_state):
                    raise ValueError(f"非法任务状态迁移：{old_state} -> {new_state}")
                values_by_name = {"state": new_state, **fields, "updated_at": time.time()}
                assignments = ", ".join(f"{key} = ?" for key in values_by_name)
                values = [*values_by_name.values(), task_id, old_state]
                cursor = self._conn.execute(
                    # assignments contains names from the fixed set above.
                    f"UPDATE tasks SET {assignments} WHERE id=? AND state=?",  # nosec B608
                    values,
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("任务状态在迁移期间发生变化")
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def request_stream(self, task_id: int) -> tuple[str, dict[str, Any] | None]:
        """Switch an idle/retryable download to explicit streaming mode."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if row is None:
                    self._conn.execute("ROLLBACK")
                    return "missing", None
                task = dict(row)
                if task["cancel_requested"] or task["state"] not in {"queued", "download_failed"}:
                    self._conn.execute("ROLLBACK")
                    return "invalid", task
                if task.get("local_path"):
                    self._conn.execute("ROLLBACK")
                    return "retained", task
                if task["state"] == "download_failed" and not can_transition(
                    "download_failed", "queued"
                ):
                    raise ValueError("状态机不允许失败任务重新排队")
                now = time.time()
                self._conn.execute(
                    """UPDATE tasks SET state='queued', transfer_mode='stream',
                    downloaded_bytes=0, uploaded_bytes=0, download_retries=0,
                    upload_retries=0, error=NULL,
                    wait_reason='用户已选择流式模式；等待资源和目的端就绪',
                    next_retry_at=0, updated_at=? WHERE id=?""",
                    (now, task_id),
                )
                updated = self._conn.execute(
                    "SELECT * FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                self._conn.execute("COMMIT")
                return "updated", dict(updated)
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def confirm_115(
        self, task_id: int
    ) -> tuple[str, dict[str, Any] | None]:
        """Atomically record a user's official-client confirmation.

        Only a completed Bot transfer can be confirmed. This prevents an
        accidental /confirm from changing a task that is still transferring.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    self._conn.execute("ROLLBACK")
                    return "missing", None
                if row["state"] == "confirmed":
                    self._conn.execute("ROLLBACK")
                    return "already", dict(row)
                if row["state"] != "completed":
                    self._conn.execute("ROLLBACK")
                    return "invalid", dict(row)
                self._conn.execute(
                    """
                    UPDATE tasks
                    SET state='confirmed', error=NULL, wait_reason=NULL,
                        next_retry_at=0, updated_at=?
                    WHERE id=? AND state='completed'
                    """,
                    (time.time(), task_id),
                )
                updated = self._conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                self._conn.execute("COMMIT")
                return "confirmed", dict(updated)
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def confirm_completed(self, limit: int = 100) -> list[int]:
        """Atomically batch-confirm recent completed transfers."""
        safe_limit = max(1, min(int(limit), 100))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT id FROM tasks WHERE state='completed' ORDER BY id DESC LIMIT ?",
                    (safe_limit,),
                ).fetchall()
                task_ids = [int(row["id"]) for row in rows]
                if task_ids:
                    placeholders = ",".join("?" for _ in task_ids)
                    self._conn.execute(
                        # placeholders are generated from the bounded ID list.
                        f"""UPDATE tasks SET state='confirmed', error=NULL,
                        wait_reason=NULL, next_retry_at=0, updated_at=?
                        WHERE state='completed' AND id IN ({placeholders})""",  # nosec B608
                        (time.time(), *task_ids),
                    )
                self._conn.execute("COMMIT")
                return task_ids
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def recover(self, download_dir: Path, *, task_id: int | None = None) -> dict[str, int]:
        recovered = {"queued": 0, "waiting_upload": 0, "failed": 0}
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE state IN (
                    'reserved', 'downloading', 'downloaded', 'waiting_upload',
                    'uploading', 'verifying', 'finalizing', 'streaming',
                    'upload_failed_retained', 'verification_failed_retained'
                )
                AND (? IS NULL OR id=?)
                """, (task_id, task_id),
            ).fetchall()
            for row in rows:
                if row["cancel_requested"]:
                    continue
                task_id = int(row["id"])
                local_path = Path(row["local_path"]) if row["local_path"] else None
                if row["transfer_mode"] == "stream":
                    self._conn.execute(
                        """
                        UPDATE tasks SET state='queued', local_path=NULL,
                            downloaded_bytes=0, upload_retries=0,
                            wait_reason='流式任务在服务重启后自动重新排队',
                            next_retry_at=0, updated_at=?
                        WHERE id=?
                        """,
                        (time.time(), task_id),
                    )
                    recovered["queued"] += 1
                    continue
                if (
                    local_path
                    and local_path.is_file()
                    and not str(local_path).endswith(".part")
                    and local_path.stat().st_size == int(row["file_size"])
                ):
                    self._conn.execute(
                        """
                        UPDATE tasks SET state='waiting_upload', error=NULL,
                            upload_retries=0, next_retry_at=0,
                            wait_reason='服务重启后恢复上传', updated_at=?
                        WHERE id=?
                        """,
                        (time.time(), task_id),
                    )
                    recovered["waiting_upload"] += 1
                    continue
                expected_final = download_dir / (
                    f"{task_id}-"
                    f"{safe_file_name(row['file_name'], str(task_id))}"
                )
                if (
                    expected_final.is_file()
                    and expected_final.stat().st_size == int(row["file_size"])
                ):
                    self._conn.execute(
                        """
                        UPDATE tasks SET state='waiting_upload',
                            local_path=?, downloaded_bytes=file_size,
                            error=NULL, upload_retries=0, next_retry_at=0,
                            wait_reason='已恢复下载完成但未登记的本地文件',
                            updated_at=?
                        WHERE id=?
                        """,
                        (str(expected_final), time.time(), task_id),
                    )
                    recovered["waiting_upload"] += 1
                    continue
                if row["state"] in {
                    "upload_failed_retained",
                    "verification_failed_retained",
                }:
                    self._conn.execute(
                        """
                        UPDATE tasks SET state='queued', local_path=NULL,
                            downloaded_bytes=0, download_retries=0,
                            upload_retries=0,
                            wait_reason='保留记录对应的本地文件已不存在，自动重新排队',
                            next_retry_at=0, updated_at=?
                        WHERE id=?
                        """,
                        (time.time(), task_id),
                    )
                    recovered["queued"] += 1
                    continue
                part_path = download_dir / f"{task_id}.part"
                if part_path.exists():
                    try:
                        part_path.unlink()
                    except OSError:
                        self._conn.execute(
                            """
                            UPDATE tasks SET state='download_failed',
                                error='重启后无法清理不完整文件', updated_at=?
                            WHERE id=?
                            """,
                            (time.time(), task_id),
                        )
                        recovered["failed"] += 1
                        continue
                self._conn.execute(
                    """
                    UPDATE tasks SET state='queued', local_path=NULL,
                        downloaded_bytes=0, download_retries=0,
                        upload_retries=0,
                        wait_reason='本地文件不完整或不存在，服务重启后重新排队',
                        updated_at=?
                    WHERE id=?
                    """,
                    (time.time(), task_id),
                )
                recovered["queued"] += 1
        return recovered
