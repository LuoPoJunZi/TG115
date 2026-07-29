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

from .config import Settings
from .db import TaskDB
from .naming import safe_file_name
from .rclone_client import RcloneClient
from .resources import AdaptiveWindow, ResourceMonitor, ResourceSnapshot

STATE_LABELS = {
    "queued": "在排队",
    "reserved": "已放行，准备下载",
    "downloading": "正在从 Telegram 下载",
    "streaming": "正在从 Telegram 流式写入 CloudDrive2",
    "downloaded": "下载完成，等待上传",
    "waiting_upload": "下载完成，等待上传",
    "uploading": "正在写入 CloudDrive2",
    "verifying": "正在校验 CloudDrive2 文件",
    "finalizing": "正在生成正式文件名",
    "cleanup_pending": "CloudDrive2 已接收，正在清理 VPS 本地文件",
    "completed": "Bot 传输已完成，115 官方端待确认",
    "confirmed": "115 官方端已由你确认",
    "download_failed": "下载失败",
    "upload_failed_retained": "上传失败，本地文件已保留",
    "verification_failed_retained": "校验失败，本地文件已保留",
    "cancelled": "已取消",
}


def state_label(state: str) -> str:
    return STATE_LABELS.get(state, state)


def format_bytes(value: float) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f}{unit}"
        size /= 1024
    return f"{size:.2f}TB"


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


class TransferService:
    def __init__(self, settings: Settings):
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
        self.rclone = RcloneClient(settings)
        self.monitor = ResourceMonitor(settings.download_dir)
        self.download_window = AdaptiveWindow(settings, "download")
        self.upload_window = AdaptiveWindow(settings, "upload")
        self.snapshot: ResourceSnapshot | None = None
        self.destination_healthy = False
        self.destination_last_checked = 0.0
        self.download_tasks: dict[int, asyncio.Task[None]] = {}
        self.upload_tasks: dict[int, asyncio.Task[None]] = {}
        self.recent_download_errors: deque[float] = deque(maxlen=50)
        self.recent_upload_errors: deque[float] = deque(maxlen=50)
        self._background: list[asyncio.Task[Any]] = []
        self._stop = asyncio.Event()
        self._finalize_lock = asyncio.Lock()
        self._register_handlers()

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
        if not self._authorized(event.sender_id):
            self.log.warning("忽略未授权用户：%s", event.sender_id)
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

    async def _handle_command(
        self, event: events.NewMessage.Event, text: str
    ) -> None:
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            await event.reply(
                "把视频或文件直接转发给我即可。\n\n"
                "/queue - 查看最近任务\n"
                "/status - 查看系统状态\n"
                "/task <编号> - 查看单个任务\n"
                "/confirm <编号> - 在 115 官方客户端确认文件\n"
                "/retry <编号> - 重试保留的失败任务\n"
                "/cancel <编号> - 取消任务"
            )
        elif command == "/queue":
            tasks = self.db.list_recent(15)
            if not tasks:
                await event.reply("当前没有任务。")
            else:
                await event.reply(
                    "最近任务：\n\n"
                    + "\n\n".join(self._format_task(task) for task in tasks)
                )
        elif command in {"/status", "/performance"}:
            await event.reply(self._format_status())
        elif command == "/task":
            await self._command_task(event, parts)
        elif command == "/confirm":
            await self._command_confirm(event, parts)
        elif command == "/retry":
            await self._command_retry(event, parts)
        elif command == "/cancel":
            await self._command_cancel(event, parts)
        else:
            await event.reply("未知命令。发送 /help 查看可用命令。")

    @staticmethod
    def _parse_task_id(parts: list[str]) -> int | None:
        if len(parts) != 2:
            return None
        try:
            return int(parts[1].lstrip("#"))
        except ValueError:
            return None

    async def _command_task(
        self, event: events.NewMessage.Event, parts: list[str]
    ) -> None:
        task_id = self._parse_task_id(parts)
        task = self.db.get(task_id) if task_id else None
        if task is None:
            await event.reply("用法：/task <任务编号>")
            return
        await event.reply(self._format_task(task, verbose=True))

    async def _command_confirm(
        self, event: events.NewMessage.Event, parts: list[str]
    ) -> None:
        task_id = self._parse_task_id(parts)
        if task_id is None:
            await event.reply(
                "用法：/confirm <任务编号>\n"
                "只在 115 官方客户端看到文件大小正常、可以打开或播放后使用。"
            )
            return
        result, task = self.db.confirm_115(task_id)
        if result == "missing" or task is None:
            await event.reply("没有找到这个任务。用法：/confirm <任务编号>")
            return
        if result == "already":
            await event.reply(f"任务 #{task_id} 已经由你确认过。")
            return
        if result == "invalid":
            await event.reply(
                f"任务当前状态为“{state_label(task['state'])}”，还不能确认。\n"
                "必须先等 Bot 传输完成，并在 115 官方客户端看到完整文件。"
            )
            return
        await event.reply(
            f"✅ 任务 #{task_id} 已标记为“115 官方端已由你确认”。\n"
            "这是你的人工确认记录；Bot 没有调用 115 官方接口复验文件。"
        )

    async def _command_retry(
        self, event: events.NewMessage.Event, parts: list[str]
    ) -> None:
        task_id = self._parse_task_id(parts)
        task = self.db.get(task_id) if task_id else None
        if task is None:
            await event.reply("用法：/retry <任务编号>")
            return
        state = task["state"]
        if state in {"upload_failed_retained", "verification_failed_retained"}:
            local_path = Path(task["local_path"] or "")
            if not local_path.is_file():
                self.db.update(
                    task_id,
                    state="queued",
                    local_path=None,
                    downloaded_bytes=0,
                    download_retries=0,
                    upload_retries=0,
                    error=None,
                    wait_reason="本地文件已不存在，用户要求重新下载",
                    next_retry_at=0,
                )
                await event.reply(f"任务 #{task_id} 已重新进入下载队列。")
                return
            self.db.update(
                task_id,
                state="waiting_upload",
                upload_retries=0,
                error=None,
                wait_reason="用户要求重试",
                next_retry_at=0,
            )
            await event.reply(f"任务 #{task_id} 已重新进入上传队列。")
        elif state == "download_failed":
            self.db.update(
                task_id,
                state="queued",
                download_retries=0,
                error=None,
                wait_reason="用户要求重试",
                next_retry_at=0,
            )
            await event.reply(f"任务 #{task_id} 已重新进入下载队列。")
        else:
            await event.reply(
                f"任务当前状态为“{state_label(state)}”，不需要手动重试。"
            )

    async def _command_cancel(
        self, event: events.NewMessage.Event, parts: list[str]
    ) -> None:
        task_id = self._parse_task_id(parts)
        task = self.db.get(task_id) if task_id else None
        if task is None:
            await event.reply("用法：/cancel <任务编号>")
            return
        if task["state"] in {
            "completed",
            "confirmed",
            "cleanup_pending",
            "cancelled",
        }:
            await event.reply(
                f"任务已经是“{state_label(task['state'])}”状态。"
            )
            return
        running = self.download_tasks.get(task_id) or self.upload_tasks.get(task_id)
        if running:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        task = self.db.get(task_id)
        if task is None:
            await event.reply("任务记录已经不存在，取消中止。")
            return
        if task["state"] in {
            "completed",
            "confirmed",
            "cleanup_pending",
            "cancelled",
        }:
            await event.reply(
                f"任务已经是“{state_label(task['state'])}”状态。"
            )
            return
        remote_path = str(task.get("remote_path") or "")
        if remote_path:
            try:
                if await self.rclone.exists(remote_path):
                    await self.rclone.remove(remote_path)
                if await self.rclone.exists(remote_path):
                    raise RuntimeError("远端文件删除后仍然存在")
            except Exception as exc:  # noqa: BLE001 - fail closed on remote cleanup
                await event.reply(
                    "CloudDrive2 远端文件暂时无法安全清理，取消中止；"
                    f"本地副本已保留：{exc}"
                )
                return
        local_path = Path(task["local_path"]) if task.get("local_path") else None
        if local_path and local_path.exists():
            try:
                local_path.unlink()
            except OSError as exc:
                await event.reply(f"无法安全删除本地文件，取消中止：{exc}")
                return
        part = self.settings.download_dir / f"{task_id}.part"
        if part.exists():
            try:
                part.unlink()
            except OSError as exc:
                await event.reply(f"无法安全删除下载临时文件，取消中止：{exc}")
                return
        self.db.update(
            task_id,
            state="cancelled",
            local_path=None,
            remote_path=None,
            downloaded_bytes=0,
            uploaded_bytes=0,
            error=None,
            wait_reason=None,
        )
        await event.reply(
            f"任务 #{task_id} 已取消，本地临时文件和本任务远端文件已清理。"
        )

    def _format_task(self, task: dict[str, Any], verbose: bool = False) -> str:
        text = (
            f"#{task['id']}｜{task['file_name']}\n"
            f"大小：{format_bytes(task['file_size'])}\n"
            f"状态：{state_label(task['state'])}"
        )
        reason = task.get("wait_reason")
        error = task.get("error")
        if reason:
            text += f"\n等待原因：{reason}"
        if error:
            text += f"\n错误：{str(error)[:500]}"
        if task.get("transfer_mode") == "stream":
            text += "\n模式：流式传输（不占用本地任务额度）"
        if verbose:
            text += (
                f"\n已下载：{format_bytes(task['downloaded_bytes'])}"
                f"\n下载重试：{task['download_retries']}"
                f"\n上传重试：{task['upload_retries']}"
            )
            if task.get("remote_path"):
                text += f"\nCloudDrive2 路径：{task['remote_path']}"
            if task["state"] == "completed":
                text += (
                    "\n115 核验：Bot 无法自动判断；在官方客户端确认后，"
                    f"发送 /confirm #{task['id']}"
                )
            elif task["state"] == "confirmed":
                text += "\n115 核验：已由你在官方客户端人工确认"
        return text

    def _format_status(self) -> str:
        counts = self.db.counts()
        counts_text = "、".join(
            f"{state_label(state)} {count}"
            for state, count in sorted(counts.items())
        ) or "无"
        used = self.db.used_local_bytes()
        snapshot = self.snapshot
        resource_text = "资源采样尚未完成"
        if snapshot:
            resource_text = (
                f"CPU 平均：{snapshot.cpu_percent:.1f}%\n"
                f"可用内存：{format_bytes(snapshot.memory_available)}\n"
                f"磁盘可用：{format_bytes(snapshot.disk_free)}\n"
                f"网络总速率：{format_bytes(snapshot.network_bytes_per_second)}/s"
            )
        return (
            "系统状态\n"
            "CloudDrive2 WebDAV："
            f"{'可写' if self.destination_healthy else '不可用或未配置完成'}\n"
            "115 官方端：Bot 不自动判断；看到完整文件后使用 /confirm <编号>\n"
            f"Bot 当前实际传输：下载/流式 {len(self.download_tasks)}，"
            f"落盘后上传 {len(self.upload_tasks)}\n"
            f"下载动态窗口：{self.download_window.value}\n"
            f"上传动态窗口：{self.upload_window.value}\n"
            f"本地额度：{format_bytes(used)} / {format_bytes(self.settings.local_budget_bytes)}\n"
            f"{resource_text}\n"
            f"任务统计：{counts_text}"
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
                now = time.time()
                if (
                    now - self.destination_last_checked
                    >= self.settings.remote_health_interval
                ):
                    self.destination_healthy = await self.rclone.healthy()
                    self.destination_last_checked = now
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
                )
                self.download_window.update(
                    self.snapshot,
                    recent_errors=self._errors_in_last_minute(
                        self.recent_download_errors
                    ),
                    destination_healthy=self.destination_healthy,
                    demand_present=download_demand,
                )
                self.upload_window.update(
                    self.snapshot,
                    recent_errors=self._errors_in_last_minute(
                        self.recent_upload_errors
                    ),
                    destination_healthy=self.destination_healthy,
                    demand_present=upload_demand,
                )
                (self.settings.data_dir / "heartbeat").touch()
            except Exception:
                self.log.exception("资源控制循环异常")
            await asyncio.sleep(self.settings.control_interval)

    async def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._start_downloads()
                await self._start_uploads()
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
        if not self.snapshot or not self.destination_healthy:
            return
        available_slots = self.download_window.value - len(self.download_tasks)
        if available_slots <= 0:
            return
        for batch in self._queued_batches(("queued",)):
            for task in batch:
                if available_slots <= 0:
                    return
                task_id = int(task["id"])
                if task_id in self.download_tasks:
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
                available_slots -= 1

    async def _start_uploads(self) -> None:
        if not self.snapshot or not self.destination_healthy:
            return
        available_slots = self.upload_window.value - len(self.upload_tasks)
        if available_slots <= 0:
            return
        for batch in self._queued_batches(
            ("cleanup_pending", "waiting_upload", "downloaded")
        ):
            for task in batch:
                if available_slots <= 0:
                    return
                task_id = int(task["id"])
                if task_id in self.upload_tasks:
                    continue
                async_task = asyncio.create_task(
                    self._upload_one(task_id), name=f"upload-{task_id}"
                )
                self.upload_tasks[task_id] = async_task
                async_task.add_done_callback(
                    lambda _, tid=task_id: self.upload_tasks.pop(tid, None)
                )
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
                last_progress_at = now

        try:
            for attempt in range(
                int(task["download_retries"]), self.settings.max_retries
            ):
                self.db.update(
                    task_id,
                    state="downloading",
                    downloaded_bytes=0,
                    download_retries=attempt,
                    local_path=str(part_path),
                    error=None,
                    wait_reason=None,
                )
                if part_path.exists():
                    part_path.unlink()
                try:
                    message = await self.client.get_messages(
                        int(task["chat_id"]), ids=int(task["message_id"])
                    )
                    if not message or not message.media:
                        raise RuntimeError("Telegram 消息或文件已经不可访问")
                    result = await self.client.download_media(
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
                    self.db.update(
                        task_id,
                        state="downloaded",
                        local_path=str(final_path),
                        downloaded_bytes=actual_size,
                    )
                    os.replace(part_path, final_path)
                    self.db.update(
                        task_id,
                        state="waiting_upload",
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
            self.db.update(
                task_id,
                state="download_failed",
                local_path=None,
                error="下载重试次数已经用完",
                wait_reason=None,
            )
            await self._notify(
                f"❌ 下载失败\n任务：#{task_id}\n"
                "不完整文件已清理，可使用 /retry 重新排队。"
            )
        except asyncio.CancelledError:
            if part_path.exists():
                part_path.unlink()
            current = self.db.get(task_id)
            if current and current["state"] != "cancelled":
                self.db.update(task_id, state="queued", local_path=None)
            raise
        except Exception as exc:
            self.log.exception("任务 #%s 下载发生未处理异常", task_id)
            if part_path.exists():
                part_path.unlink()
            self.db.update(
                task_id,
                state="download_failed",
                local_path=None,
                error=str(exc)[:1000],
            )

    async def _finalize_stream_remote(
        self,
        task_id: int,
        task: dict[str, Any],
        remote_path: str,
        safe_name: str,
    ) -> None:
        final_remote = remote_path
        if remote_path.startswith(".uploading-"):
            async with self._finalize_lock:
                final_remote = await self._choose_remote_final(safe_name, task_id)
                self.db.update(
                    task_id,
                    state="finalizing",
                    remote_path=final_remote,
                )
                await self.rclone.move(remote_path, final_remote)
        final_size = await self.rclone.remote_size(final_remote)
        if final_size != int(task["file_size"]):
            await self.rclone.remove(final_remote)
            raise RuntimeError(
                f"流式改名后远端大小错误：{final_size} != {task['file_size']}"
            )
        await self._complete_cloud_receive(
            task_id, task, None, final_remote
        )

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
                    current = self.db.get(task_id) or task
                    recorded_remote = str(current.get("remote_path") or "")
                    if recorded_remote and await self.rclone.exists(recorded_remote):
                        recorded_size = await self.rclone.remote_size(recorded_remote)
                        if recorded_size == int(task["file_size"]):
                            await self._finalize_stream_remote(
                                task_id,
                                task,
                                recorded_remote,
                                safe_name,
                            )
                            return
                        if recorded_remote.startswith(".uploading-"):
                            await self.rclone.remove(recorded_remote)
                    self.db.update(
                        task_id,
                        state="streaming",
                        transfer_mode="stream",
                        local_path=None,
                        remote_path=remote_temp,
                        downloaded_bytes=0,
                        uploaded_bytes=0,
                        download_retries=attempt,
                        error=None,
                        wait_reason=None,
                    )
                    message = await self.client.get_messages(
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
                            last_progress_at = now

                    await self.client.download_media(
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
                    self.db.update(task_id, state="verifying")
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
            if retained_remote.startswith(".uploading-"):
                retained_remote = ""
            self.db.update(
                task_id,
                state="download_failed",
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
            if recorded_remote.startswith(".uploading-"):
                await self.rclone.remove(recorded_remote)
                recorded_remote = ""
            if current and current["state"] != "cancelled":
                self.db.update(
                    task_id,
                    state="queued",
                    local_path=None,
                    remote_path=recorded_remote or None,
                    downloaded_bytes=0,
                    uploaded_bytes=0,
                    wait_reason="流式任务已暂停并自动重新排队",
                )
            raise
        except Exception as exc:
            self.log.exception("任务 #%s 流式传输发生未处理异常", task_id)
            self.db.update(
                task_id,
                state="download_failed",
                local_path=None,
                error=str(exc)[:1000],
            )

    async def _choose_remote_final(self, file_name: str, task_id: int) -> str:
        if not await self.rclone.exists(file_name):
            return file_name
        path = Path(file_name)
        candidate = f"{path.stem} (task-{task_id}){path.suffix}"
        suffix = 2
        while await self.rclone.exists(candidate):
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
        # Persist the verified remote result before deleting the local copy.
        # A restart between these operations can then resume cleanup without
        # re-uploading or creating a duplicate remote file.
        self.db.update(
            task_id,
            state="cleanup_pending",
            remote_path=final_remote,
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
        self.db.update(
            task_id,
            state="completed",
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
            "在 115 官方客户端看到文件大小正常且可以打开或播放后，"
            f"发送 /confirm #{task_id}。"
        )

    async def _upload_one(self, task_id: int) -> None:
        task = self.db.get(task_id)
        if not task:
            return
        local_path = Path(task["local_path"]) if task.get("local_path") else None
        if task.get("state") == "cleanup_pending":
            recorded_remote = str(task.get("remote_path") or "")
            try:
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
                    self.db.update(
                        task_id,
                        state="queued",
                        local_path=None,
                        downloaded_bytes=0,
                        uploaded_bytes=0,
                        wait_reason=(
                            "流式任务的远端文件无法确认，已自动重新排队"
                        ),
                        next_retry_at=0,
                    )
                    return
                self.db.update(
                    task_id,
                    state="verification_failed_retained",
                    error="远端正式文件和 VPS 本地副本都无法确认，已停止自动操作",
                    wait_reason=None,
                )
                return
            self.db.update(
                task_id,
                state="waiting_upload",
                remote_path=None,
                error="远端正式文件不存在，保留本地副本并重新上传",
                wait_reason=None,
                next_retry_at=0,
            )
            task = self.db.get(task_id) or task
        if local_path is None or not local_path.is_file():
            self.db.update(
                task_id,
                state="queued",
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
                self.db.update(
                    task_id,
                    state="verification_failed_retained",
                    error=f"{error}；且无法清理：{exc}"[:1000],
                )
                return
            self.db.update(
                task_id,
                state="queued",
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
                    current = self.db.get(task_id) or task
                    recorded_remote = str(current.get("remote_path") or "")
                    if (
                        recorded_remote
                        and not recorded_remote.startswith(".uploading-")
                        and await self.rclone.exists(recorded_remote)
                    ):
                        recorded_size = await self.rclone.remote_size(recorded_remote)
                        if recorded_size == int(task["file_size"]):
                            await self._complete_cloud_receive(
                                task_id, task, local_path, recorded_remote
                            )
                            return
                    self.db.update(
                        task_id,
                        state="uploading",
                        remote_path=remote_temp,
                    )
                    await self.rclone.upload(local_path, remote_temp)
                    self.db.update(task_id, state="verifying")
                    remote_size = await self.rclone.remote_size(remote_temp)
                    if remote_size != int(task["file_size"]):
                        raise RuntimeError(
                            f"远端大小错误：{remote_size} != {task['file_size']}"
                        )
                    async with self._finalize_lock:
                        final_remote = await self._choose_remote_final(
                            safe_name, task_id
                        )
                        self.db.update(
                            task_id,
                            state="finalizing",
                            remote_path=final_remote,
                        )
                        await self.rclone.move(remote_temp, final_remote)
                    final_size = await self.rclone.remote_size(final_remote)
                    if final_size != int(task["file_size"]):
                        await self.rclone.remove(final_remote)
                        raise RuntimeError(
                            f"改名后远端大小错误：{final_size} != {task['file_size']}"
                        )
                    await self._complete_cloud_receive(
                        task_id, task, local_path, final_remote
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
            self.db.update(
                task_id,
                state="upload_failed_retained",
                error="上传重试次数已经用完；本地完整文件已保留",
            )
            await self._notify(
                f"❌ 上传失败但本地文件已保留\n任务：#{task_id}\n"
                "修复 CloudDrive2 后使用 /retry 重试。"
            )
        except asyncio.CancelledError:
            await self.rclone.remove(remote_temp)
            current = self.db.get(task_id)
            if current and current["state"] != "cancelled":
                self.db.update(task_id, state="waiting_upload")
            raise
        except Exception as exc:
            self.log.exception("任务 #%s 上传发生未处理异常", task_id)
            self.db.update(
                task_id,
                state="upload_failed_retained",
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
            asyncio.create_task(self._scheduler_loop(), name="scheduler-loop"),
        ]
        (self.settings.data_dir / "heartbeat").touch()
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
