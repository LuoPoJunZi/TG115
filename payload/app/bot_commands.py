from __future__ import annotations

import asyncio
import secrets
import time
from pathlib import Path
from typing import Any

from telethon import events

from .states import FAILED_STATES, STATE_LABELS

ORPHAN_CLEANUP_LIMIT = 100
ORPHAN_CLEANUP_TTL_SECONDS = 5 * 60
TERMINAL_TASK_STATES = {"completed", "confirmed", "cancelled"}


def state_label(state: str) -> str:
    return STATE_LABELS.get(state, state)


def format_bytes(value: float) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f}{unit}"
        size /= 1024
    return f"{size:.2f}TB"


class CommandMixin:
    """Telegram command routing and user-facing status rendering."""

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
                "/doctor - 只读诊断（不写远端）\n"
                "/pause /resume - 暂停／恢复新任务调度\n"
                "/task <编号> - 查看单个任务\n"
                "/watch <编号> - 每 5 秒更新同一条进度消息\n"
                "/stream <编号> - 将排队或下载失败任务改为流式传输\n"
                "/orphans - 只读巡检远端临时文件；clean 需二次确认\n"
                "/confirm <编号|all> - 记录单个或批量 115 人工确认\n"
                "/retry <编号|all> - 重试失败任务（all 每次最多 100 个）\n"
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
        elif command == "/watch":
            task_id = self._parse_task_id(parts)
            task = self.db.get(task_id) if task_id else None
            if task is None:
                await event.reply("用法：/watch <任务编号>")
            else:
                message = await event.reply(self._format_task(task, verbose=True))
                self._watch_messages[task_id] = message
        elif command in {"/pause", "/resume"}:
            self.db.set_paused(command == "/pause")
            await event.reply(
                "已暂停启动新传输；活动任务继续完成，暂停状态在重启后保留。"
                if command == "/pause" else "已恢复调度；仍需满足远端和资源安全条件。"
            )
        elif command == "/doctor":
            await event.reply(self._format_doctor())
        elif command == "/stream":
            await self._command_stream(event, parts)
        elif command == "/orphans":
            await self._command_orphans(event, parts)
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
        if len(parts) == 2 and parts[1].lower() == "all":
            task_ids = self.db.confirm_completed(limit=100)
            if not task_ids:
                await event.reply("当前没有等待人工确认的已完成任务。")
                return
            await event.reply(
                f"✅ 已批量确认 {len(task_ids)} 个任务。\n"
                "这是你在 115 官方客户端集中核验后的人工记录；"
                "如果还有更多，可再次发送 /confirm all。"
            )
            return
        task_id = self._parse_task_id(parts)
        if task_id is None:
            await event.reply(
                "用法：/confirm <任务编号|all>\n"
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

    async def _command_stream(
        self, event: events.NewMessage.Event, parts: list[str]
    ) -> None:
        task_id = self._parse_task_id(parts)
        if task_id is None:
            await event.reply("用法：/stream <任务编号>")
            return
        result, task = self.db.request_stream(task_id)
        if result == "missing" or task is None:
            await event.reply("没有找到这个任务。用法：/stream <任务编号>")
        elif result == "retained":
            await event.reply("该任务已有本地完整文件；请保留可恢复上传能力，不切换流式模式。")
        elif result == "invalid":
            await event.reply(
                f"任务当前状态为“{state_label(task['state'])}”，不能切换流式模式。"
            )
        else:
            await event.reply(
                f"任务 #{task_id} 已改为流式传输并重新排队。\n"
                "流式模式不保留完整本地副本，中断后通常需要从头重传。"
            )

    def _is_orphan_staging(self, task_id: int, remote_path: str) -> bool:
        task = self.db.get(task_id)
        if task is None:
            return True
        recorded_paths = {
            str(task.get("remote_temp_path") or ""),
            str(task.get("remote_final_path") or ""),
            str(task.get("remote_path") or ""),
        }
        if remote_path in recorded_paths:
            return False
        # Any non-terminal task with the same id may be about to claim its
        # deterministic staging name. Keep it even when the path is not yet saved.
        return task["state"] in TERMINAL_TASK_STATES

    async def _find_orphan_staging(self) -> list[tuple[int, str]]:
        inspect_staging = getattr(self.rclone, "list_staging_objects", None)
        if not callable(inspect_staging):
            raise NotImplementedError
        objects = await asyncio.wait_for(inspect_staging(), timeout=45)
        return [
            (task_id, remote_path)
            for task_id, remote_path in objects
            if self._is_orphan_staging(task_id, remote_path)
        ]

    async def _command_orphans(
        self, event: events.NewMessage.Event, parts: list[str] | None = None
    ) -> None:
        parts = parts or ["/orphans"]
        if len(parts) not in {1, 2, 3} or (
            len(parts) >= 2 and parts[1].lower() != "clean"
        ):
            await event.reply(
                "用法：/orphans（只读巡检）或 /orphans clean（二次确认清理）"
            )
            return
        if len(parts) == 3:
            await self._confirm_orphan_cleanup(event, parts[2])
            return
        try:
            orphans = await self._find_orphan_staging()
        except NotImplementedError:
            await event.reply("当前目的端不支持远端临时文件巡检。")
            return
        except Exception as exc:  # noqa: BLE001 - external diagnostic boundary
            self.log.warning("远端临时文件巡检失败：%s", exc)
            await event.reply("远端临时文件巡检失败；未执行任何删除操作。")
            return
        if not orphans:
            await event.reply("只读巡检完成：没有发现未被活动任务跟踪的 .uploading-* 文件。")
            return
        shown = "、".join(f"#{task_id}" for task_id, _ in orphans[:20])
        suffix = "（仅显示前 20 项）" if len(orphans) > 20 else ""
        if len(parts) == 1:
            await event.reply(
                f"只读巡检发现 {len(orphans)} 个疑似遗留临时文件：{shown}{suffix}\n"
                "本命令未删除任何内容；如需清理，发送 /orphans clean 获取一次性确认码。"
            )
            return
        planned = tuple(orphans[:ORPHAN_CLEANUP_LIMIT])
        token = secrets.token_hex(3)
        self._orphan_cleanup_plan = {
            "token": token,
            "expires_at": time.monotonic() + ORPHAN_CLEANUP_TTL_SECONDS,
            "objects": planned,
        }
        limited = (
            f"；本次只计划前 {ORPHAN_CLEANUP_LIMIT} 个"
            if len(orphans) > ORPHAN_CLEANUP_LIMIT else ""
        )
        await event.reply(
            f"待清理 {len(planned)} 个疑似遗留临时文件{limited}。\n"
            f"5 分钟内发送 /orphans clean {token} 二次确认。\n"
            "确认时会重新扫描；已关联活动任务的文件不会删除，确认码只能使用一次。"
        )

    async def _confirm_orphan_cleanup(
        self, event: events.NewMessage.Event, token: str
    ) -> None:
        plan = getattr(self, "_orphan_cleanup_plan", None)
        if not plan or time.monotonic() > float(plan["expires_at"]):
            self._orphan_cleanup_plan = None
            await event.reply("清理确认码不存在或已过期；请重新发送 /orphans clean。")
            return
        if not secrets.compare_digest(str(plan["token"]), token):
            await event.reply("清理确认码不正确；未删除任何内容。")
            return
        # Consume before any mutation so retries cannot replay a partially used plan.
        self._orphan_cleanup_plan = None
        try:
            current = set(await self._find_orphan_staging())
        except Exception as exc:  # noqa: BLE001 - fail closed on rescan
            self.log.warning("清理前重新巡检远端临时文件失败：%s", exc)
            await event.reply("清理前重新巡检失败；未删除任何内容，请重新发起巡检。")
            return
        deleted = 0
        protected = 0
        for task_id, remote_path in plan["objects"]:
            if (task_id, remote_path) not in current or not self._is_orphan_staging(
                task_id, remote_path
            ):
                protected += 1
                continue
            try:
                await asyncio.wait_for(self.rclone.remove(remote_path), timeout=60)
                if await asyncio.wait_for(self.rclone.exists(remote_path), timeout=45):
                    raise RuntimeError("远端文件删除后仍然存在")
            except Exception as exc:  # noqa: BLE001 - fail closed on remote cleanup
                self.log.warning("远端遗留临时文件清理失败：%s", exc)
                await event.reply(
                    f"清理在删除 {deleted} 个后停止：无法确认下一项已安全删除。\n"
                    f"另有 {protected} 个已消失或受任务保护；请重新 /orphans 巡检。"
                )
                return
            deleted += 1
        await event.reply(
            f"清理完成：已删除并复查 {deleted} 个遗留临时文件；"
            f"跳过 {protected} 个已消失或已受任务保护的文件。"
        )

    async def _command_retry(
        self, event: events.NewMessage.Event, parts: list[str]
    ) -> None:
        if len(parts) == 2 and parts[1].lower() == "all":
            tasks = self.db.list_states(FAILED_STATES, limit=100)
            count = sum(
                self._retry_task(task)
                for task in tasks
                if not task.get("cancel_requested")
            )
            await event.reply(
                f"已重新排队 {count} 个失败任务；取消清理未完成的任务不参与批量重试。"
            )
            return
        task_id = self._parse_task_id(parts)
        task = self.db.get(task_id) if task_id else None
        if task is None:
            await event.reply("用法：/retry <任务编号>")
            return
        if self._retry_task(task):
            await event.reply(f"任务 #{task_id} 已重新进入队列。")
        else:
            await event.reply(
                f"任务当前状态为“{state_label(task['state'])}”，不需要手动重试。"
            )

    def _retry_task(self, task: dict[str, Any]) -> bool:
        task_id = int(task["id"])
        if task_id in getattr(self, "_cancelling", set()):
            return False
        state = task["state"]
        if task.get("cancel_requested"):
            # A manual retry withdraws the cancellation intent, not its paths.
            self.db.update(task_id, cancel_requested=0)
            self.db.recover(self.settings.download_dir, task_id=task_id)
            task = self.db.get(task_id) or task
            state = task["state"]
        if state in {"upload_failed_retained", "verification_failed_retained"}:
            local_path = Path(task["local_path"] or "")
            if not local_path.is_file():
                self.db.transition(
                    task_id,
                    "queued",
                    local_path=None,
                    downloaded_bytes=0,
                    download_retries=0,
                    upload_retries=0,
                    error=None,
                    wait_reason="本地文件已不存在，用户要求重新下载",
                    next_retry_at=0,
                )
                return True
            self.db.transition(
                task_id,
                "waiting_upload",
                upload_retries=0,
                error=None,
                wait_reason="用户要求重试",
                next_retry_at=0,
            )
            return True
        if state == "download_failed":
            self.db.transition(
                task_id,
                "queued",
                download_retries=0,
                error=None,
                wait_reason="用户要求重试",
                next_retry_at=0,
            )
            return True
        return state in {"queued", "waiting_upload"}

    async def _command_cancel(
        self, event: events.NewMessage.Event, parts: list[str]
    ) -> None:
        task_id = self._parse_task_id(parts)
        if not hasattr(self, "_cancelling"):
            self._cancelling: set[int | None] = set()
        if task_id in self._cancelling:
            await event.reply("这个任务正在取消清理，请等待结果。")
            return
        self._cancelling.add(task_id)
        try:
            await self._cancel_task_command(event, parts)
        finally:
            self._cancelling.discard(task_id)

    async def _cancel_task_command(
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
            await event.reply(f"任务已经是“{state_label(task['state'])}”状态。")
            return
        self.db.update(
            task_id,
            cancel_requested=1,
            wait_reason="取消清理待完成；失败后可再次 /cancel 或手动 /retry",
        )
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
            await event.reply(f"任务已经是“{state_label(task['state'])}”状态。")
            return
        remote_paths = dict.fromkeys(
            str(task.get(key) or "")
            for key in ("remote_temp_path", "remote_final_path", "remote_path")
        )
        for remote_path in filter(None, remote_paths):
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
        self.db.transition(
            task_id,
            "cancelled",
            local_path=None,
            remote_path=None,
            remote_temp_path=None,
            remote_final_path=None,
            cancel_requested=0,
            downloaded_bytes=0,
            uploaded_bytes=0,
            error=None,
            wait_reason=None,
        )
        await event.reply(
            f"任务 #{task_id} 已取消，本地临时文件和本任务远端文件已清理。"
        )

    def _format_task(self, task: dict[str, Any], verbose: bool = False) -> str:
        label = state_label(task["state"])
        if task.get("transfer_mode") == "stream" and task["state"] in FAILED_STATES:
            label = "流式传输失败；无完整本地副本，远端路径记录已保留"
        text = (
            f"#{task['id']}｜{task['file_name']}\n"
            f"大小：{format_bytes(task['file_size'])}\n"
            f"状态：{label}"
        )
        reason = task.get("wait_reason")
        error = task.get("error")
        if reason:
            text += f"\n等待原因：{reason}"
        if error:
            text += f"\n错误：{str(error)[:500]}"
        if task.get("transfer_mode") == "stream":
            text += "\n模式：流式传输（不占用本地任务额度）"
        progress = getattr(self, "_progress", {}).get(int(task["id"]))
        if progress and task["state"] in {"downloading", "uploading", "streaming"}:
            age = time.monotonic() - progress["at"]
            speed = progress["speed"] if age <= 20 else 0
            label = (
                "管道送入（非远端确认）"
                if progress["stage"] == "stream"
                else "本阶段传输"
            )
            percent = min(100, 100 * progress["bytes"] / max(1, task["file_size"]))
            text += (
                f"\n{label}：{percent:.1f}%｜{format_bytes(speed)}/s"
                f"｜耗时 {int(time.monotonic() - progress['started'])} 秒"
            )
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
                    "\n115 核验：Bot 无法自动判断；人工记录是可选的，"
                    f"可发送 /confirm #{task['id']}，或集中核验后发送 /confirm all"
                )
            elif task["state"] == "confirmed":
                text += "\n115 核验：已由你在官方客户端人工确认"
            if hasattr(self.db, "events"):
                history = self.db.events(int(task["id"]), limit=4)
                if history:
                    text += "\n最近状态：" + " → ".join(
                        state_label(row["new_state"])
                        for row in reversed(history)
                    )
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
        if self._destination_ready():
            scope = getattr(self, "destination_scope", "unknown")
            if scope == "target":
                destination_text = "目标目录可访问（未验证写入）"
            elif scope == "root_fallback":
                destination_text = "WebDAV 根目录可访问；目标目录尚未创建（未验证写入）"
            elif scope == "root":
                destination_text = "WebDAV 根目录可访问（未验证写入）"
            else:
                destination_text = "目录可访问，探测范围未知（未验证写入）"
        else:
            destination_text = "不可用、检查过期或未配置完成"
        return (
            "系统状态\n"
            f"CloudDrive2 WebDAV：{destination_text}\n"
            f"目录检查距今：{int(max(0, time.time() - self.destination_last_checked)) if self.destination_last_checked else '尚未完成'} 秒\n"
            f"队列调度：{'已暂停' if self.db.is_paused() else '运行中'}\n"
            "115 官方端：Bot 不自动判断；如需记录，可用 /confirm <编号|all>\n"
            f"Bot 当前实际传输：下载/流式 {len(self.download_tasks)}，"
            f"落盘后上传 {len(self.upload_tasks)}\n"
            f"下载动态窗口：{self.download_window.value}\n"
            f"上传动态窗口：{self.upload_window.value}\n"
            f"本地额度：{format_bytes(used)} / {format_bytes(self.settings.local_budget_bytes)}\n"
            f"{resource_text}\n"
            f"Telegram 落盘下载：{format_bytes(self._stage_rate('download'))}/s\n"
            f"WebDAV 上传：{format_bytes(self._stage_rate('upload'))}/s\n"
            f"流式管道送入：{format_bytes(self._stage_rate('stream'))}/s（非远端确认）\n"
            f"任务统计：{counts_text}"
        )

    def _format_doctor(self) -> str:
        checked = self.destination_last_checked
        age = f"{max(0, int(time.time() - checked))} 秒前" if checked else "尚未完成"
        return (
            "只读诊断（使用本进程采样，不创建远端测试文件）\n"
            f"资源采样：{'新鲜' if self._sample_fresh() else '过期或未就绪；停止放行'}\n"
            f"远端目录检查：{age}\n"
            f"目的端：{'可访问' if self._destination_ready() else '未就绪或检查已过期'}\n"
            f"检查说明：{self.destination_error or '目录探测通过，不代表可写'}\n"
            f"下载窗口：{self.download_window.value}，上传窗口：{self.upload_window.value}\n"
            "写入、改名和删除权限请主动执行 manage.sh verify 验收。\n"
            "115 官方端仍需人工确认；敏感配置不会显示在此处。"
        )
