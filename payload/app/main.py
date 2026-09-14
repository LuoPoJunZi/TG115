from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from telethon import TelegramClient, events

from .bot_commands import CommandMixin, format_bytes
from .config import Settings
from .db import TaskDB
from .interfaces import Destination, DestinationProbe, MediaSource
from .naming import safe_file_name
from .rclone_client import RcloneClient
from .resources import AdaptiveWindow, ResourceMonitor, ResourceSnapshot
from .states import FAILED_STATES


def setup_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        settings.log_dir / "tg115.log",
        maxBytes=10 * 1024**2,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)


class TransferService(CommandMixin):
    def __init__(
        self, settings: Settings, *, source: MediaSource | None = None,
        destination: Destination | None = None,
    ):
        self.settings = settings
        setup_logging(settings)
        self.log = logging.getLogger("tg115")
        self.db = TaskDB(settings.data_dir / "tg115.db")
        self.client = TelegramClient(
            str(settings.data_dir / "bot"),
            settings.api_id,
            settings.api_hash,
            request_retries=5,
            connection_retries=10,
            retry_delay=3,
            auto_reconnect=True,
        )
        self._source = source
        self.rclone = destination or RcloneClient(settings)
        self.monitor = ResourceMonitor(settings.download_dir)
        self.download_window = AdaptiveWindow(settings, "download")
        self.upload_window = AdaptiveWindow(settings, "upload")
        self.snapshot: ResourceSnapshot | None = None
        self.destination_healthy = False
        self.destination_last_checked = 0.0
        self.destination_scope = "unknown"
        self.destination_error = "尚未检查"
        self._progress: dict[int, dict[str, Any]] = {}
        self._watch_messages: dict[int, Any] = {}
        self.download_tasks: dict[int, asyncio.Task[None]] = {}
        self.upload_tasks: dict[int, asyncio.Task[None]] = {}
        self.recent_download_errors: deque[float] = deque(maxlen=50)
        self.recent_upload_errors: deque[float] = deque(maxlen=50)
        self._background: list[asyncio.Task[Any]] = []
        self._stop = asyncio.Event()
        self._finalize_lock = asyncio.Lock()
        self._orphan_cleanup_plan: dict[str, Any] | None = None
        self._register_handlers()

    @property
    def source(self) -> MediaSource:
        return getattr(self, "_source", None) or self.client

    def _sample_fresh(self) -> bool:
        return bool(self.snapshot and 0 <= time.time() - self.snapshot.sampled_at
                    <= max(10, self.settings.control_interval * 3))

    def _destination_ready(self) -> bool:
        return (self.destination_healthy and 0 <= time.time() - self.destination_last_checked
                <= self.settings.remote_health_interval + 45)

    def _stream_count(self) -> int:
        return sum(1 for task_id in self.download_tasks
                   if (self.db.get(task_id) or {}).get("transfer_mode") == "stream")

    def _record_progress(
        self, task_id: int, stage: str, count: int, speed: float | None = None,
    ) -> None:
        if not hasattr(self, "_progress"):
            self._progress = {}
        now = time.monotonic()
        old = self._progress.get(task_id)
        if not old or old["stage"] != stage or count < old["bytes"]:
            old = {"stage": stage, "bytes": 0, "at": now, "started": now}
        measured = (max(0, count - old["bytes"]) / (now - old["at"])) if now > old["at"] else 0
        self._progress[task_id] = {
            "stage": stage, "bytes": count, "at": now, "started": old["started"],
            "speed": max(0, speed if speed is not None else measured),
        }

    def _stage_rate(self, stage: str) -> float:
        return sum(p["speed"] for p in getattr(self, "_progress", {}).values()
                   if p["stage"] == stage and time.monotonic() - p["at"] <= 20)

    def _register_handlers(self) -> None:
        self.client.add_event_handler(self._on_message, events.NewMessage(incoming=True))

    def _authorized(self, sender_id: int | None) -> bool:
        return sender_id == self.settings.allowed_user_id

    @staticmethod
    def _media_key(message: Any) -> str | None:
        document = getattr(message, "document", None)
        document_id = getattr(document, "id", None)
        if document_id is not None:
            return f"document:{document_id}"
        photo = getattr(message, "photo", None)
        photo_id = getattr(photo, "id", None)
        if photo_id is not None:
            return f"photo:{photo_id}"
        return None

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        if not self._authorized(event.sender_id) or not getattr(event, "is_private", False):
            self.log.warning("忽略未授权或非私聊消息")
            return

        text = (event.raw_text or "").strip()
        if text.startswith("/"):
            await self._handle_command(event, text)
            return

        message = event.message
        if not message.media or not message.file:
            await event.reply("这条消息没有可下载的文件。请转发视频或文档。")
            return
        file_size = int(message.file.size or 0)
        if file_size <= 0:
            await event.reply("无法读取文件大小，任务没有进入队列。")
            return
        fallback = f"telegram-{message.id}{message.file.ext or '.bin'}"
        file_name = safe_file_name(message.file.name or "", fallback)
        task, created = self.db.create_task(
            chat_id=int(event.chat_id),
            message_id=int(message.id),
            sender_id=int(event.sender_id),
            media_key=self._media_key(message),
            file_name=file_name,
            file_size=file_size,
        )
        if not created:
            await event.reply(
                f"这个文件已经登记过。\n{self._format_task(task)}"
            )
            return
        await event.reply(
            "✅ 已接收并持久化\n"
            f"任务：#{task['id']}\n"
            f"文件：{file_name}\n"
            f"大小：{format_bytes(file_size)}\n"
            "状态：在排队\n"
            "无需重新转发，系统会自动处理。"
        )

    def _errors_in_last_minute(self, errors: deque[float]) -> int:
        cutoff = time.time() - 60
        while errors and errors[0] < cutoff:
            errors.popleft()
        return len(errors)

    async def _enforce_disk_emergency(self) -> None:
        if (
            not self.snapshot
            or self.snapshot.disk_free > self.settings.min_free_disk_bytes
            or not self.download_tasks
        ):
            return
        active = list(self.download_tasks.items())
        self.log.error(
            "磁盘已触及安全线，暂停 %s 个下载并清理不完整文件",
            len(active),
        )
        for _, task in active:
            task.cancel()
        await asyncio.gather(
            *(task for _, task in active),
            return_exceptions=True,
        )
        requeued = 0
        for task_id, _ in active:
            current = self.db.get(task_id)
            if current and current["state"] == "queued":
                self.db.update(
                    task_id,
                    downloaded_bytes=0,
                    wait_reason="磁盘触及安全线，已自动暂停并重新排队",
                )
                requeued += 1
        if requeued:
            await self._notify(
                f"⚠️ 磁盘触及安全线，已暂停 {requeued} 个下载并重新排队；"
                "空间恢复后会自动继续。"
            )

    async def _resource_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.snapshot = self.monitor.sample()
                await self._enforce_disk_emergency()
                counts = self.db.counts()
                download_demand = bool(
                    self.download_tasks
                    or counts.get("queued")
                    or counts.get("reserved")
                    or counts.get("downloading")
                    or counts.get("streaming")
                )
                upload_demand = bool(
                    self.upload_tasks
                    or counts.get("downloaded")
                    or counts.get("waiting_upload")
                    or counts.get("cleanup_pending")
                    or counts.get("uploading")
                    or counts.get("verifying")
                    or counts.get("finalizing")
                    or self._stream_count()
                )
                self.download_window.update(
                    self.snapshot,
                    recent_errors=self._errors_in_last_minute(
                        self.recent_download_errors
                    ),
                    destination_healthy=self._destination_ready(),
                    demand_present=download_demand,
                    throughput=self._stage_rate("download") + self._stage_rate("stream"),
                    active_count=len(self.download_tasks),
                    backlog_pressure=self.db.used_local_bytes() >= self.settings.local_budget_bytes * 0.8,
                )
                self.upload_window.update(
                    self.snapshot,
                    recent_errors=self._errors_in_last_minute(
                        self.recent_upload_errors
                    ),
                    destination_healthy=self._destination_ready(),
                    demand_present=upload_demand,
                    throughput=self._stage_rate("upload") + self._stage_rate("stream"),
                    active_count=len(self.upload_tasks) + self._stream_count(),
                )
                (self.settings.data_dir / "resource-heartbeat").touch()
            except Exception:
                self.snapshot = None
                self.log.exception("资源控制循环异常")
            await asyncio.sleep(self.settings.control_interval)

    async def _destination_loop(self) -> None:
        while not self._stop.is_set():
            try:
                probe_method = getattr(self.rclone, "probe", None)
                if callable(probe_method):
                    probe = await asyncio.wait_for(probe_method(), 30)
                else:
                    accessible = await asyncio.wait_for(self.rclone.healthy(), 30)
                    probe = DestinationProbe(
                        accessible,
                        "unknown",
                        "目录探测通过，不代表可写"
                        if accessible else "目录访问失败，请核对凭据与网络",
                    )
                self.destination_healthy = probe.accessible
                self.destination_scope = probe.scope
                self.destination_error = probe.detail
            except TimeoutError:
                self.destination_healthy = False
                self.destination_scope = "unknown"
                self.destination_error = "目录探测超时"
            except Exception:
                self.destination_healthy = False
                self.destination_scope = "unknown"
                self.destination_error = "目录探测异常，请查看本机最近日志"
                self.log.exception("目的端探测异常")
            self.destination_last_checked = time.time()
            await asyncio.sleep(self.settings.remote_health_interval)

    async def _watch_loop(self) -> None:
        while not self._stop.is_set():
            for task_id, message in list(self._watch_messages.items()):
                task = self.db.get(task_id)
                if task is None:
                    self._watch_messages.pop(task_id, None)
                    continue
                text = self._format_task(task, verbose=True)
                try:
                    if text != getattr(message, "raw_text", None):
                        await asyncio.wait_for(message.edit(text), timeout=5)
                except Exception:  # noqa: BLE001 - Telegram progress is best-effort
                    self._watch_messages.pop(task_id, None)
                    self.log.warning("进度订阅更新失败，已停止订阅 #%s", task_id)
                if task["state"] in {*FAILED_STATES, "completed", "confirmed", "cancelled"}:
                    self._watch_messages.pop(task_id, None)
            await asyncio.sleep(5)

    async def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            try:
                # Drain retained files first; streaming also consumes upload slots.
                await self._start_uploads()
                await self._start_downloads()
                (self.settings.data_dir / "heartbeat").touch()
            except Exception:
                self.log.exception("调度循环异常")
            await asyncio.sleep(1)

    def _queued_batches(self, states: tuple[str, ...]):
        after_id = 0
        while True:
            batch = self.db.list_states(
                states,
                limit=200,
                ready_only=True,
                after_id=after_id,
            )
            if not batch:
                return
            yield batch
            after_id = int(batch[-1]["id"])

    async def _start_downloads(self) -> None:
        if not self.destination_healthy or not self._sample_fresh() or not self._destination_ready():
            return
        if self.db.is_paused():
            return
        available_slots = self.download_window.value - len(self.download_tasks)
        if available_slots <= 0:
            return
        for batch in self._queued_batches(("queued",)):
            await asyncio.sleep(0)
            if not self._sample_fresh() or not self._destination_ready() or self.db.is_paused():
                return
            for task in batch:
                if available_slots <= 0:
                    return
                task_id = int(task["id"])
                if task_id in self.download_tasks:
                    continue
                if ((task.get("transfer_mode") == "stream"
                     or task["file_size"] > self.settings.local_budget_bytes)
                        and len(self.upload_tasks) + self._stream_count() >= self.upload_window.value):
                    self.db.update(task_id, wait_reason="流式任务等待共享上传窗口")
                    continue
                reserved, reason = self.db.reserve(
                    task_id,
                    budget_bytes=self.settings.local_budget_bytes,
                    current_free_bytes=self.snapshot.disk_free,
                    minimum_free_bytes=self.settings.min_free_disk_bytes,
                )
                if not reserved:
                    self.log.debug("任务 #%s 暂不放行：%s", task_id, reason)
                    continue
                async_task = asyncio.create_task(
                    self._download_one(task_id), name=f"download-{task_id}"
                )
                self.download_tasks[task_id] = async_task
                async_task.add_done_callback(
                    lambda _, tid=task_id: self.download_tasks.pop(tid, None)
                )
                async_task.add_done_callback(lambda _, tid=task_id: self._progress.pop(tid, None))
                available_slots -= 1

    async def _start_uploads(self) -> None:
        if not self.destination_healthy or not self._sample_fresh() or not self._destination_ready():
            return
        available_slots = self.upload_window.value - len(self.upload_tasks) - self._stream_count()
        if available_slots <= 0:
            return
        states = ("cleanup_pending",) if self.db.is_paused() else ("cleanup_pending", "waiting_upload", "downloaded")
        for batch in self._queued_batches(states):
            await asyncio.sleep(0)
            if not self._sample_fresh() or not self._destination_ready():
                return
            for task in batch:
                if available_slots <= 0:
                    return
                task_id = int(task["id"])
                current = self.db.get(task_id)
                if not current or current.get("cancel_requested") or current["state"] not in states:
                    continue
                if self.db.is_paused() and task["state"] != "cleanup_pending":
                    continue
                if task_id in self.upload_tasks:
                    continue
                async_task = asyncio.create_task(
                    self._upload_one(task_id), name=f"upload-{task_id}"
                )
                self.upload_tasks[task_id] = async_task
                async_task.add_done_callback(
                    lambda _, tid=task_id: self.upload_tasks.pop(tid, None)
                )
                async_task.add_done_callback(lambda _, tid=task_id: self._progress.pop(tid, None))
                available_slots -= 1

    async def _download_one(self, task_id: int) -> None:
        task = self.db.get(task_id)
        if not task:
            return
        if task.get("transfer_mode") == "stream":
            await self._stream_one(task_id)
            return
        part_path = self.settings.download_dir / f"{task_id}.part"
        final_name = f"{task_id}-{safe_file_name(task['file_name'], str(task_id))}"
        final_path = self.settings.download_dir / final_name
        last_progress_at = 0.0

        def progress(received: int, total: int) -> None:
            nonlocal last_progress_at
            now = time.monotonic()
            if now - last_progress_at >= 1 or received >= total:
                self.db.update(task_id, downloaded_bytes=int(received))
                self._record_progress(task_id, "download", int(received))
                last_progress_at = now

        try:
            for attempt in range(
                int(task["download_retries"]), self.settings.max_retries
            ):
                self.db.transition(
                    task_id,
                    "downloading",
                    downloaded_bytes=0,
                    download_retries=attempt,
                    local_path=str(part_path),
                    error=None,
                    wait_reason=None,
                )
                if part_path.exists():
                    part_path.unlink()
                try:
                    message = await self.source.get_messages(
                        int(task["chat_id"]), ids=int(task["message_id"])
                    )
                    if not message or not message.media:
                        raise RuntimeError("Telegram 消息或文件已经不可访问")
                    result = await self.source.download_media(
                        message, file=str(part_path), progress_callback=progress
                    )
                    if not result or not part_path.is_file():
                        raise RuntimeError("Telethon 没有生成下载文件")
                    actual_size = part_path.stat().st_size
                    if actual_size != int(task["file_size"]):
                        raise RuntimeError(
                            f"本地大小错误：{actual_size} != {task['file_size']}"
                        )
                    # Persist the final path before the atomic rename. A crash
                    # after the rename can then recover the completed file.
                    self.db.transition(
                        task_id,
                        "downloaded",
                        local_path=str(final_path),
                        downloaded_bytes=actual_size,
                    )
                    os.replace(part_path, final_path)
                    self.db.transition(
                        task_id,
                        "waiting_upload",
                        local_path=str(final_path),
                        downloaded_bytes=actual_size,
                        download_retries=attempt,
                        error=None,
                        wait_reason=None,
                        next_retry_at=0,
                    )
                    await self._notify(
                        f"⬇️ 下载完成，等待上传\n任务：#{task_id}\n"
                        f"文件：{task['file_name']}"
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - retry external I/O
                    self.recent_download_errors.append(time.time())
                    self.log.warning(
                        "任务 #%s 下载第 %s 次失败：%s", task_id, attempt + 1, exc
                    )
                    self.db.update(
                        task_id,
                        download_retries=attempt + 1,
                        error=str(exc)[:1000],
                    )
                    if attempt + 1 < self.settings.max_retries:
                        await asyncio.sleep(min(60, 2 ** (attempt + 1)))
            if part_path.exists():
                part_path.unlink()
            self.db.transition(
                task_id,
                "download_failed",
                local_path=None,
                error="下载重试次数已经用完",
                wait_reason=None,
            )
            await self._notify(
                f"❌ 下载失败\n任务：#{task_id}\n"
                "不完整文件已清理，可使用 /retry 重新排队。"
            )
        except asyncio.CancelledError:
            current = self.db.get(task_id)
            if current and current["state"] not in {"cancelled", "completed", "confirmed", "cleanup_pending"}:
                if final_path.is_file() and final_path.stat().st_size == int(task["file_size"]):
                    self.db.transition(task_id, "waiting_upload", local_path=str(final_path))
                else:
                    if not current.get("cancel_requested") and part_path.exists():
                        part_path.unlink()
                    self.db.transition(task_id, "queued", local_path=None, downloaded_bytes=0)
            raise
        except Exception as exc:
            self.log.exception("任务 #%s 下载发生未处理异常", task_id)
            if part_path.exists():
                part_path.unlink()
            self.db.transition(
                task_id,
                "download_failed",
                local_path=None,
                error=str(exc)[:1000],
            )

    async def _finalize_stream_remote(
        self,
        task_id: int,
        task: dict[str, Any],
        remote_path: str,
        safe_name: str,
        local_path: Path | None = None,
    ) -> None:
        final_remote = remote_path
        if remote_path.startswith(".uploading-"):
            async with self._finalize_lock:
                current = self.db.get(task_id) or task
                final_remote = str(current.get("remote_final_path") or "")
                if not final_remote:
                    final_remote = await self._choose_remote_final(safe_name, task_id)
                elif await self.rclone.exists(final_remote):
                    raise RuntimeError("预留正式路径已经存在；保留数据，等待复核")
                self.db.transition(
                    task_id,
                    "finalizing",
                    remote_path=final_remote,
                    remote_temp_path=remote_path,
                    remote_final_path=final_remote,
                )
                await self.rclone.move(remote_path, final_remote)
        final_size = await self.rclone.remote_size(final_remote)
        if final_size != int(task["file_size"]):
            await self.rclone.remove(final_remote)
            raise RuntimeError(
                f"流式改名后远端大小错误：{final_size} != {task['file_size']}"
            )
        await self._complete_cloud_receive(
            task_id, task, local_path, final_remote
        )

    async def _resume_remote(
        self, task_id: int, task: dict[str, Any], local_path: Path | None,
        safe_name: str,
    ) -> bool:
        current = self.db.get(task_id) or task
        legacy = str(current.get("remote_path") or "")
        final = str(current.get("remote_final_path") or
                    (legacy if legacy and not legacy.startswith(".uploading-") else ""))
        temp = str(current.get("remote_temp_path") or
                   (legacy if legacy.startswith(".uploading-") else ""))
        if final and await self.rclone.exists(final):
            if await self.rclone.remote_size(final) != int(task["file_size"]):
                raise RuntimeError("记录的正式文件大小不符；停止覆盖并保留本地数据")
            await self._complete_cloud_receive(task_id, task, local_path, final)
            return True
        if temp and await self.rclone.exists(temp):
            if await self.rclone.remote_size(temp) == int(task["file_size"]):
                await self._finalize_stream_remote(task_id, task, temp, safe_name, local_path)
                return True
            await self.rclone.remove(temp)
            if await self.rclone.exists(temp):
                raise RuntimeError("不完整远端临时文件无法清理；停止重传")
        return False

    async def _stream_one(self, task_id: int) -> None:
        task = self.db.get(task_id)
        if not task:
            return
        safe_name = safe_file_name(task["file_name"], f"task-{task_id}.bin")
        remote_temp = f".uploading-{task_id}-{safe_name}"
        stream = None
        try:
            for attempt in range(
                int(task["download_retries"]), self.settings.max_retries
            ):
                try:
                    if await self._resume_remote(task_id, task, None, safe_name):
                        return
                    self.db.transition(
                        task_id,
                        "streaming",
                        transfer_mode="stream",
                        local_path=None,
                        remote_path=remote_temp,
                        remote_temp_path=remote_temp,
                        downloaded_bytes=0,
                        uploaded_bytes=0,
                        download_retries=attempt,
                        error=None,
                        wait_reason=None,
                    )
                    message = await self.source.get_messages(
                        int(task["chat_id"]), ids=int(task["message_id"])
                    )
                    if not message or not message.media:
                        raise RuntimeError("Telegram 消息或文件已经不可访问")
                    stream = await self.rclone.open_upload_stream(
                        remote_temp, int(task["file_size"])
                    )
                    last_progress_at = 0.0

                    def progress(received: int, total: int) -> None:
                        nonlocal last_progress_at
                        now = time.monotonic()
                        if now - last_progress_at >= 1 or received >= total:
                            self.db.update(
                                task_id,
                                downloaded_bytes=int(received),
                                uploaded_bytes=int(received),
                            )
                            self._record_progress(task_id, "stream", int(received))
                            last_progress_at = now

                    await self.source.download_media(
                        message,
                        file=stream,
                        progress_callback=progress,
                    )
                    streamed_size = stream.tell()
                    if streamed_size != int(task["file_size"]):
                        raise RuntimeError(
                            f"流式读取大小错误：{streamed_size} "
                            f"!= {task['file_size']}"
                        )
                    await stream.finish()
                    self.db.transition(task_id, "verifying")
                    remote_size = await self.rclone.remote_size(remote_temp)
                    if remote_size != int(task["file_size"]):
                        raise RuntimeError(
                            f"流式远端大小错误：{remote_size} "
                            f"!= {task['file_size']}"
                        )
                    await self._finalize_stream_remote(
                        task_id, task, remote_temp, safe_name
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - retry external I/O
                    if stream is not None:
                        await stream.abort()
                        stream = None
                    await self.rclone.remove(remote_temp)
                    self.recent_download_errors.append(time.time())
                    self.recent_upload_errors.append(time.time())
                    self.log.warning(
                        "任务 #%s 流式传输第 %s 次失败：%s",
                        task_id,
                        attempt + 1,
                        exc,
                    )
                    self.db.update(
                        task_id,
                        download_retries=attempt + 1,
                        error=str(exc)[:1000],
                    )
                    if attempt + 1 < self.settings.max_retries:
                        await asyncio.sleep(min(120, 3 ** (attempt + 1)))
            current = self.db.get(task_id) or task
            retained_remote = str(current.get("remote_path") or "")
            self.db.transition(
                task_id,
                "download_failed",
                local_path=None,
                remote_path=retained_remote or None,
                error="流式传输重试次数已经用完；使用 /retry 可重新开始",
                wait_reason=None,
            )
            await self._notify(
                f"❌ 流式传输失败\n任务：#{task_id}\n"
                "大文件不会占用本地任务额度，可使用 /retry 重新开始。"
            )
        except asyncio.CancelledError:
            if stream is not None:
                await stream.abort()
            current = self.db.get(task_id)
            recorded_remote = (
                str(current.get("remote_path") or "") if current else ""
            )
            if current and current["state"] not in {"cancelled", "cleanup_pending", "completed", "confirmed"}:
                self.db.transition(
                    task_id,
                    "queued",
                    local_path=None,
                    remote_path=recorded_remote or None,
                    downloaded_bytes=0,
                    uploaded_bytes=0,
                    wait_reason="流式任务已暂停并自动重新排队",
                )
            raise
        except Exception as exc:
            self.log.exception("任务 #%s 流式传输发生未处理异常", task_id)
            self.db.transition(
                task_id,
                "download_failed",
                local_path=None,
                error=str(exc)[:1000],
            )

    async def _choose_remote_final(self, file_name: str, task_id: int) -> str:
        def claimed(path: str) -> bool:
            check = getattr(getattr(self, "db", None), "remote_claimed", None)
            return bool(check and check(path, task_id))

        if not claimed(file_name) and not await self.rclone.exists(file_name):
            return file_name
        path = Path(file_name)
        candidate = f"{path.stem} (task-{task_id}){path.suffix}"
        suffix = 2
        while claimed(candidate) or await self.rclone.exists(candidate):
            candidate = f"{path.stem} (task-{task_id}-{suffix}){path.suffix}"
            suffix += 1
        return candidate

    async def _complete_cloud_receive(
        self,
        task_id: int,
        task: dict[str, Any],
        local_path: Path | None,
        final_remote: str,
    ) -> None:
        current = self.db.get(task_id) or task
        temp = str(current.get("remote_temp_path") or "")
        if temp and temp != final_remote and await self.rclone.exists(temp):
            await self.rclone.remove(temp)
            if await self.rclone.exists(temp):
                raise RuntimeError("改名后临时文件仍存在；保留本地副本等待安全清理")
        # Persist the verified remote result before deleting the local copy.
        # A restart between these operations can then resume cleanup without
        # re-uploading or creating a duplicate remote file.
        self.db.transition(
            task_id,
            "cleanup_pending",
            remote_path=final_remote,
            remote_final_path=final_remote,
            remote_temp_path=None,
            uploaded_bytes=int(task["file_size"]),
            error=None,
            wait_reason="远端文件已校验，正在清理 VPS 本地副本",
            next_retry_at=0,
        )
        if local_path is not None:
            try:
                local_path.unlink(missing_ok=True)
            except OSError as exc:
                self.db.update(
                    task_id,
                    error=f"远端已接收，但 VPS 本地文件暂时无法清理：{exc}"[:1000],
                    wait_reason="远端已接收；等待自动重试清理 VPS 本地副本",
                    next_retry_at=time.time() + 60,
                )
                self.log.warning("任务 #%s 的本地副本清理失败：%s", task_id, exc)
                return
        self.db.transition(
            task_id,
            "completed",
            local_path=None,
            error=None,
            wait_reason=None,
            next_retry_at=0,
        )
        await self._notify(
            "✅ Bot 传输已完成，CloudDrive2 已接收\n"
            f"任务：#{task_id}\n"
            f"文件：{task['file_name']}\n"
            f"CloudDrive2 路径：{final_remote}\n"
            "这不代表 Bot 已自动验证 115 官方端。\n"
            "人工确认记录不是必需操作；集中核验后可发送一次 /confirm all。"
        )

    async def _upload_one(self, task_id: int) -> None:
        task = self.db.get(task_id)
        if not task or task.get("cancel_requested") or task.get("state") in {"completed", "confirmed", "cancelled"}:
            return
        local_path = Path(task["local_path"]) if task.get("local_path") else None
        if task.get("state") == "cleanup_pending":
            recorded_remote = str(task.get("remote_path") or "")
            try:
                if (recorded_remote and await self.rclone.exists(recorded_remote)
                        and await self.rclone.remote_size(recorded_remote) != int(task["file_size"])):
                    self.db.transition(
                        task_id, "verification_failed_retained",
                        error="已接收的正式文件大小发生变化；停止自动操作，保留路径和本地副本",
                        wait_reason=None,
                    )
                    return
                if (
                    recorded_remote
                    and await self.rclone.exists(recorded_remote)
                    and await self.rclone.remote_size(recorded_remote)
                    == int(task["file_size"])
                ):
                    await self._complete_cloud_receive(
                        task_id, task, local_path, recorded_remote
                    )
                    return
            except Exception as exc:  # noqa: BLE001 - retry external I/O
                self.db.update(
                    task_id,
                    error=f"检查已接收的远端文件失败：{exc}"[:1000],
                    wait_reason="等待 CloudDrive2 恢复后继续本地清理",
                    next_retry_at=time.time() + 60,
                )
                return
            if local_path is None or not local_path.is_file():
                if task.get("transfer_mode") == "stream":
                    self.db.transition(
                        task_id,
                        "queued",
                        local_path=None,
                        downloaded_bytes=0,
                        uploaded_bytes=0,
                        wait_reason=(
                            "流式任务的远端文件无法确认，已自动重新排队"
                        ),
                        next_retry_at=0,
                    )
                    return
                self.db.transition(
                    task_id,
                    "verification_failed_retained",
                    error="远端正式文件和 VPS 本地副本都无法确认，已停止自动操作",
                    wait_reason=None,
                )
                return
            self.db.transition(
                task_id,
                "waiting_upload",
                remote_path=None,
                remote_final_path=None,
                error="远端正式文件不存在，保留本地副本并重新上传",
                wait_reason=None,
                next_retry_at=0,
            )
            task = self.db.get(task_id) or task
        if local_path is None or not local_path.is_file():
            self.db.transition(
                task_id,
                "queued",
                local_path=None,
                downloaded_bytes=0,
                download_retries=0,
                upload_retries=0,
                error="数据库记录的本地完整文件不存在",
                wait_reason="本地文件不存在，已自动重新排队下载",
                next_retry_at=0,
            )
            return
        actual_local_size = local_path.stat().st_size
        if actual_local_size != int(task["file_size"]):
            error = (
                f"本地文件大小错误：{actual_local_size} != {task['file_size']}"
            )
            try:
                local_path.unlink()
            except OSError as exc:
                self.db.transition(
                    task_id,
                    "verification_failed_retained",
                    error=f"{error}；且无法清理：{exc}"[:1000],
                )
                return
            self.db.transition(
                task_id,
                "queued",
                local_path=None,
                downloaded_bytes=0,
                download_retries=0,
                upload_retries=0,
                error=error,
                wait_reason="本地文件不完整，已自动重新排队下载",
                next_retry_at=0,
            )
            await self._notify(
                f"⚠️ 本地文件校验失败，已自动重新下载\n任务：#{task_id}"
            )
            return
        safe_name = safe_file_name(task["file_name"], f"task-{task_id}.bin")
        remote_temp = f".uploading-{task_id}-{safe_name}"
        try:
            for attempt in range(
                int(task["upload_retries"]), self.settings.max_retries
            ):
                self.db.update(
                    task_id,
                    upload_retries=attempt,
                    error=None,
                    wait_reason=None,
                )
                try:
                    if await self._resume_remote(task_id, task, local_path, safe_name):
                        return
                    self.db.transition(
                        task_id,
                        "uploading",
                        remote_path=remote_temp,
                        remote_temp_path=remote_temp,
                    )
                    self._record_progress(task_id, "upload", 0, 0)

                    def progress(count: int, speed: float) -> None:
                        self.db.update(task_id, uploaded_bytes=min(count, int(task["file_size"])))
                        self._record_progress(task_id, "upload", count, speed)

                    await self.rclone.upload(local_path, remote_temp, progress=progress)
                    self.db.transition(task_id, "verifying")
                    remote_size = await self.rclone.remote_size(remote_temp)
                    if remote_size != int(task["file_size"]):
                        raise RuntimeError(
                            f"远端大小错误：{remote_size} != {task['file_size']}"
                        )
                    await self._finalize_stream_remote(
                        task_id, task, remote_temp, safe_name, local_path
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - retry external I/O
                    self.recent_upload_errors.append(time.time())
                    self.log.warning(
                        "任务 #%s 上传第 %s 次失败：%s", task_id, attempt + 1, exc
                    )
                    self.db.update(
                        task_id,
                        upload_retries=attempt + 1,
                        error=str(exc)[:1000],
                    )
                    if attempt + 1 < self.settings.max_retries:
                        await asyncio.sleep(min(120, 3 ** (attempt + 1)))
            await self.rclone.remove(remote_temp)
            self.db.transition(
                task_id,
                "upload_failed_retained",
                error="上传重试次数已经用完；本地完整文件已保留",
            )
            await self._notify(
                f"❌ 上传失败但本地文件已保留\n任务：#{task_id}\n"
                "修复 CloudDrive2 后使用 /retry 重试。"
            )
        except asyncio.CancelledError:
            current = self.db.get(task_id)
            if current and current["state"] not in {"cancelled", "cleanup_pending", "completed", "confirmed"}:
                self.db.transition(task_id, "waiting_upload")
            raise
        except Exception as exc:
            self.log.exception("任务 #%s 上传发生未处理异常", task_id)
            self.db.transition(
                task_id,
                "upload_failed_retained",
                error=str(exc)[:1000],
            )

    async def _notify(self, message: str) -> None:
        try:
            await self.client.send_message(self.settings.allowed_user_id, message)
        except Exception:
            self.log.exception("发送 Bot 通知失败")

    async def start(self) -> None:
        recovered = self.db.recover(self.settings.download_dir)
        self.log.info("重启恢复结果：%s", recovered)
        await self.client.start(bot_token=self.settings.bot_token)
        me = await self.client.get_me()
        self.log.info("Telegram Bot 已登录：@%s", me.username)
        self._background = [
            asyncio.create_task(self._resource_loop(), name="resource-loop"),
            asyncio.create_task(self._destination_loop(), name="destination-loop"),
            asyncio.create_task(self._scheduler_loop(), name="scheduler-loop"),
            asyncio.create_task(self._watch_loop(), name="watch-loop"),
        ]
        await self._notify("🤖 Telegram → 115 服务已启动。发送 /status 查看状态。")

    async def stop(self) -> None:
        self._stop.set()
        all_tasks = (
            self._background
            + list(self.download_tasks.values())
            + list(self.upload_tasks.values())
        )
        for task in all_tasks:
            task.cancel()
        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)
        await self.client.disconnect()
        self.db.close()

    async def run(self) -> None:
        await self.start()
        await self._stop.wait()


async def async_main() -> None:
    settings = Settings.from_env()
    service = TransferService(settings)
    loop = asyncio.get_running_loop()
    for signame in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signame, service._stop.set)
        except NotImplementedError:
            pass
    try:
        await service.run()
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(async_main())
