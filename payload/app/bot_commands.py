from __future__ import annotations

import asyncio
import secrets
import time
import unicodedata
from pathlib import Path
from typing import Any

from telethon import Button, events
from telethon.errors import MessageNotModifiedError
from telethon.tl import functions, types

from .states import FAILED_STATES, STATE_LABELS

ORPHAN_CLEANUP_LIMIT = 100
ORPHAN_CLEANUP_TTL_SECONDS = 5 * 60
TASK_ACTION_TTL_SECONDS = 5 * 60
QUEUE_PAGE_SIZE = 5
MAX_QUEUE_PAGE = 10_000
TERMINAL_TASK_STATES = {"completed", "confirmed", "cancelled"}
BOT_MENU_COMMANDS = (
    ("status", "查看系统状态"),
    ("queue", "查看最近任务"),
    ("pause", "暂停任务调度"),
    ("resume", "恢复任务调度"),
    ("doctor", "运行系统诊断"),
    ("orphans", "检查临时文件"),
    ("help", "查看使用帮助"),
)


def state_label(state: str) -> str:
    # Keep the persisted legacy state distinct for recovery compatibility, but
    # do not expose the retired manual-confirmation workflow in normal Bot UI.
    if state == "confirmed":
        state = "completed"
    return STATE_LABELS.get(state, state)


def format_status_counts(counts: dict[str, int]) -> str:
    groups = (
        ("排队", ("queued", "reserved")),
        ("下载中", ("downloading",)),
        ("待上传", ("downloaded", "waiting_upload")),
        (
            "上传中",
            ("uploading", "streaming", "verifying", "finalizing", "cleanup_pending"),
        ),
        ("Bot 完成", ("completed", "confirmed")),
        ("失败", tuple(FAILED_STATES)),
        ("已取消", ("cancelled",)),
    )
    parts: list[str] = []
    known: set[str] = set()
    for label, states in groups:
        known.update(states)
        total = sum(counts.get(state, 0) for state in states)
        if total:
            parts.append(f"{label} {total}")
    parts.extend(
        f"{state_label(state)} {count}"
        for state, count in sorted(counts.items())
        if state not in known and count
    )
    return "，".join(parts) or "无任务"


def format_bytes(value: float) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f}{unit}"
        size /= 1024
    return f"{size:.2f}TB"


def format_rate(value: float) -> str:
    return "0B/s" if value <= 0 else f"{format_bytes(value)}/s"


def truncate_display(text: str, max_width: int = 32) -> str:
    """Truncate a Telegram label by approximate rendered character width."""
    if max_width < 2:
        raise ValueError("max_width 必须至少为 2")
    current = 0
    result: list[str] = []
    for index, char in enumerate(text):
        width = 0 if unicodedata.combining(char) else (
            2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        )
        if current + width > max_width or (
            index < len(text) - 1 and current + width >= max_width
        ):
            return "".join(result) + "…"
        result.append(char)
        current += width
    return "".join(result)


class CommandMixin:
    """Telegram command routing and user-facing status rendering."""

    async def _register_bot_menu(self) -> None:
        """Install the native Telegram command menu without blocking startup."""
        commands = [
            types.BotCommand(command=command, description=description)
            for command, description in BOT_MENU_COMMANDS
        ]
        try:
            await self.client(
                functions.bots.SetBotCommandsRequest(
                    scope=types.BotCommandScopeDefault(),
                    lang_code="",
                    commands=commands,
                )
            )
            await self.client(
                functions.bots.SetBotMenuButtonRequest(
                    user_id=types.InputUserEmpty(),
                    button=types.BotMenuButtonCommands(),
                )
            )
        except Exception as exc:  # noqa: BLE001 - optional Telegram UI setup
            self.log.warning("注册 Telegram 原生命令菜单失败：%s", exc)

    @staticmethod
    def _format_help() -> str:
        return (
            "使用帮助\n\n"
            "提交文件：直接发送或转发视频、文档等文件\n"
            "状态查看：/status、/queue [页码]\n"
            "任务查看：/task <编号>\n"
            "进度跟踪：/watch <编号>\n"
            "失败重试：/retry <编号|all>\n"
            "取消任务：/cancel <编号>\n"
            "流式传输：/stream <编号>\n"
            "调度控制：/pause、/resume\n"
            "运行诊断：/doctor\n"
            "临时巡检：/orphans\n"
            "快捷菜单：使用输入框左侧原生命令菜单"
        )

    @staticmethod
    def _main_buttons() -> list[list[Any]]:
        return [
            [
                Button.inline("系统状态", data=b"menu:status"),
                Button.inline("最近任务", data=b"queue:1"),
            ],
            [
                Button.inline("暂停调度", data=b"control:pause"),
                Button.inline("恢复调度", data=b"control:resume"),
            ],
            [
                Button.inline("运行诊断", data=b"menu:doctor"),
                Button.inline("临时巡检", data=b"menu:orphans"),
            ],
        ]

    @staticmethod
    def _secondary_buttons(refresh_data: bytes) -> list[list[Any]]:
        return [
            [
                Button.inline("刷新页面", data=refresh_data),
                Button.inline("返回菜单", data=b"menu:home"),
            ]
        ]

    def _task_buttons(self, task: dict[str, Any] | None) -> list[list[Any]]:
        if task is None:
            return self._secondary_buttons(b"queue:1")
        task_id = int(task["id"])
        rows: list[list[Any]] = [
            [
                Button.inline("刷新进度", data=f"task:view:{task_id}".encode()),
                Button.inline("最近任务", data=b"queue:1"),
            ]
        ]
        actions: list[Any] = []
        if task["state"] in FAILED_STATES or task.get("cancel_requested"):
            actions.append(
                Button.inline("重新排队", data=f"task:retry:{task_id}".encode())
            )
        if (
            task["state"] in {"queued", "download_failed"}
            and not task.get("local_path")
            and task.get("transfer_mode") != "stream"
            and not task.get("cancel_requested")
        ):
            actions.append(
                Button.inline("切换流式", data=f"task:stream:{task_id}".encode())
            )
        if actions:
            rows.append(actions)
        if task["state"] not in {
            "completed",
            "confirmed",
            "cleanup_pending",
            "cancelled",
        }:
            rows.append(
                [Button.inline("取消任务", data=f"task:cancel:{task_id}".encode())]
            )
        rows.append([Button.inline("返回菜单", data=b"menu:home")])
        return rows

    @staticmethod
    def _format_operation_result(
        *, status: str, operation: str, note: str, task: dict[str, Any] | None = None
    ) -> str:
        lines = ["操作结果", "", f"执行状态：{status}"]
        if task is not None:
            lines.extend(
                (
                    f"任务编号：#{task['id']}",
                    f"执行操作：{operation}",
                    f"当前状态：{state_label(task['state'])}",
                )
            )
        else:
            lines.append(f"执行操作：{operation}")
        lines.append(f"后续说明：{note}")
        return "\n".join(lines)

    @staticmethod
    def _parse_queue_page(parts: list[str]) -> int | None:
        if len(parts) == 1:
            return 1
        if len(parts) != 2:
            return None
        try:
            page = int(parts[1])
        except ValueError:
            return None
        return page if 1 <= page <= MAX_QUEUE_PAGE else None

    def _queue_page(
        self, page: int
    ) -> tuple[str, list[dict[str, Any]], bool]:
        offset = (page - 1) * QUEUE_PAGE_SIZE
        fetched = self.db.list_recent(QUEUE_PAGE_SIZE + 1, offset=offset)
        tasks = fetched[:QUEUE_PAGE_SIZE]
        has_next = len(fetched) > QUEUE_PAGE_SIZE
        if not tasks:
            return (
                (
                    "最近任务\n\n"
                    f"当前页码：第 {page} 页\n"
                    "任务列表：这一页没有任务"
                ),
                [],
                False,
            )
        text = (
            "最近任务\n\n"
            f"当前页码：第 {page} 页\n\n"
            + "\n\n".join(self._format_queue_task(task) for task in tasks)
        )
        return text, tasks, has_next

    def _queue_buttons(
        self, page: int, tasks: list[dict[str, Any]], has_next: bool
    ) -> list[list[Any]]:
        task_buttons = [
            Button.inline(
                f"查看 #{task['id']}", data=f"task:view:{task['id']}".encode()
            )
            for task in tasks
        ]
        rows = [task_buttons[index:index + 2] for index in range(0, len(task_buttons), 2)]
        navigation: list[Any] = []
        if page > 1:
            navigation.append(Button.inline("上一页", data=f"queue:{page - 1}".encode()))
        if has_next:
            navigation.append(Button.inline("下一页", data=f"queue:{page + 1}".encode()))
        if navigation:
            rows.append(navigation)
        rows.append(
            [
                Button.inline("系统状态", data=b"menu:status"),
                Button.inline("返回菜单", data=b"menu:home"),
            ]
        )
        return rows

    async def _reply_queue(self, event: Any, page: int) -> None:
        text, _, _ = self._queue_page(page)
        await event.reply(text)

    async def _edit_callback(
        self, event: Any, text: str, buttons: list[list[Any]]
    ) -> None:
        try:
            await event.edit(text, buttons=buttons)
        except MessageNotModifiedError:
            return

    async def _callback_command(
        self,
        event: Any,
        handler: Any,
        parts: list[str],
        task_id: int,
    ) -> None:
        owner = self

        class EditReplyProxy:
            async def reply(self, text: str, **_: Any) -> None:
                task = owner.db.get(task_id)
                await owner._edit_callback(event, text, owner._task_buttons(task))

        await handler(EditReplyProxy(), parts)

    async def _show_orphans_callback(self, event: Any) -> None:
        try:
            orphans = await self._find_orphan_staging()
        except NotImplementedError:
            text = "临时巡检\n\n巡检结果：当前目的端不支持巡检"
        except Exception as exc:  # noqa: BLE001 - external diagnostic boundary
            self.log.warning("远端临时文件巡检失败：%s", exc)
            text = "临时巡检\n\n巡检结果：远端巡检失败\n安全处理：没有执行删除操作"
        else:
            if not orphans:
                text = (
                    "临时巡检\n\n"
                    "巡检结果：没有发现疑似遗留文件\n"
                    "安全处理：本次只读检查，没有删除内容"
                )
            else:
                shown = "、".join(f"#{task_id}" for task_id, _ in orphans[:20])
                suffix = "（仅显示前 20 项）" if len(orphans) > 20 else ""
                text = (
                    "临时巡检\n\n"
                    f"巡检结果：发现 {len(orphans)} 个疑似遗留文件\n"
                    f"任务编号：{shown}{suffix}\n"
                    "安全处理：本次没有删除任何内容\n"
                    "清理方式：发送 /orphans clean 获取一次性确认码"
                )
        await self._edit_callback(
            event, text, self._secondary_buttons(b"menu:orphans")
        )

    async def _handle_callback(self, event: Any) -> None:
        try:
            data = bytes(event.data).decode("ascii")
            parts = data.split(":")
            if data in {"menu:home", "menu:status", "menu:doctor", "menu:orphans"}:
                action: tuple[str, Any] = (data, None)
            elif len(parts) == 2 and parts[0] == "queue":
                page = int(parts[1])
                if not 1 <= page <= MAX_QUEUE_PAGE:
                    raise ValueError
                action = ("queue", page)
            elif len(parts) == 2 and parts[0] == "control" and parts[1] in {"pause", "resume"}:
                action = ("control", parts[1])
            elif (
                len(parts) == 3
                and parts[0] == "task"
                and parts[1] in {"view", "retry", "stream", "cancel"}
            ):
                task_id = int(parts[2])
                if task_id <= 0:
                    raise ValueError
                action = (f"task:{parts[1]}", task_id)
            elif (
                len(parts) == 4
                and parts[0] == "task"
                and parts[1] in {"cancel_yes", "cancel_no"}
                and parts[3]
            ):
                task_id = int(parts[2])
                if task_id <= 0:
                    raise ValueError
                action = (f"task:{parts[1]}", (task_id, parts[3]))
            else:
                raise ValueError
        except (UnicodeDecodeError, ValueError, TypeError):
            await event.answer("按钮已经失效，请发送 /help 重新打开菜单。", alert=True)
            return

        await event.answer()
        name, value = action
        if name == "menu:home":
            await self._edit_callback(event, self._format_help(), self._main_buttons())
        elif name == "menu:status":
            await self._edit_callback(event, self._format_status(), self._main_buttons())
        elif name == "menu:doctor":
            await self._edit_callback(
                event,
                self._format_doctor(),
                self._secondary_buttons(b"menu:doctor"),
            )
        elif name == "menu:orphans":
            await self._show_orphans_callback(event)
        elif name == "queue":
            text, tasks, has_next = self._queue_page(value)
            await self._edit_callback(
                event, text, self._queue_buttons(value, tasks, has_next)
            )
        elif name == "control":
            paused = value == "pause"
            self.db.set_paused(paused)
            text = self._format_operation_result(
                status="操作成功",
                operation="暂停新任务调度" if paused else "恢复任务调度",
                note=(
                    "活动任务继续完成，暂停状态会在重启后保留"
                    if paused
                    else "新任务仍需满足目的端与资源安全条件"
                ),
            )
            await self._edit_callback(event, text, self._main_buttons())
        elif name == "task:view":
            task = self.db.get(value)
            text = (
                self._format_task(task, verbose=True)
                if task
                else "任务详情\n\n查询结果：没有找到这个任务"
            )
            await self._edit_callback(event, text, self._task_buttons(task))
        elif name in {"task:retry", "task:stream"}:
            command = "/retry" if name == "task:retry" else "/stream"
            handler = self._command_retry if name == "task:retry" else self._command_stream
            await self._callback_command(event, handler, [command, str(value)], value)
        elif name == "task:cancel":
            task = self.db.get(value)
            if task is None:
                await self._edit_callback(
                    event,
                    "确认取消\n\n查询结果：没有找到这个任务",
                    self._secondary_buttons(b"queue:1"),
                )
                return
            if task["state"] in {
                "completed", "confirmed", "cleanup_pending", "cancelled",
            }:
                await self._edit_callback(
                    event, self._format_task(task, verbose=True), self._task_buttons(task)
                )
                return
            token = secrets.token_hex(3)
            confirmations = getattr(self, "_cancel_confirmations", {})
            confirmations[int(task["id"])] = {
                "token": token,
                "expires_at": time.monotonic() + TASK_ACTION_TTL_SECONDS,
            }
            self._cancel_confirmations = confirmations
            text = (
                "确认取消\n\n"
                f"任务编号：#{task['id']}\n"
                f"文件名称：{truncate_display(str(task['file_name']))}\n"
                f"当前状态：{state_label(task['state'])}\n"
                "操作影响：清理本地临时文件及本任务远端文件\n"
                "安全说明：远端无法确认删除时会中止并保留本地数据\n"
                "有效时间：请在 5 分钟内确认"
            )
            buttons = [[
                Button.inline(
                    "确认取消", data=f"task:cancel_yes:{task['id']}:{token}".encode()
                ),
                Button.inline(
                    "放弃操作", data=f"task:cancel_no:{task['id']}:{token}".encode()
                ),
            ]]
            await self._edit_callback(event, text, buttons)
        else:
            task_id, token = value
            plan = getattr(self, "_cancel_confirmations", {}).get(task_id)
            valid = bool(
                plan
                and time.monotonic() <= float(plan["expires_at"])
                and secrets.compare_digest(str(plan["token"]), token)
            )
            if not valid:
                if plan and time.monotonic() > float(plan["expires_at"]):
                    self._cancel_confirmations.pop(task_id, None)
                await self._edit_callback(
                    event,
                    "确认取消\n\n确认状态：按钮不存在或已经过期\n"
                    "后续操作：请重新打开任务详情",
                    self._task_buttons(self.db.get(task_id)),
                )
                return
            self._cancel_confirmations.pop(task_id, None)
            if name == "task:cancel_no":
                task = self.db.get(task_id)
                await self._edit_callback(
                    event,
                    self._format_operation_result(
                        status="已经放弃",
                        operation="取消任务",
                        note="任务保持原状态，没有删除任何内容",
                        task=task,
                    ),
                    self._task_buttons(task),
                )
            else:
                await self._callback_command(
                    event,
                    self._command_cancel,
                    ["/cancel", str(task_id)],
                    task_id,
                )

    async def _handle_command(
        self, event: events.NewMessage.Event, text: str
    ) -> None:
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            await event.reply(self._format_help())
        elif command == "/queue":
            page = self._parse_queue_page(parts)
            if page is None:
                await event.reply("使用说明\n\n正确格式：/queue [页码]")
            else:
                await self._reply_queue(event, page)
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
                self._format_operation_result(
                    status="操作成功",
                    operation=(
                        "暂停新任务调度" if command == "/pause" else "恢复任务调度"
                    ),
                    note=(
                        "活动任务继续完成，暂停状态会在重启后保留"
                        if command == "/pause"
                        else "新任务仍需满足目的端与资源安全条件"
                    ),
                )
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
            await event.reply("使用说明\n\n正确格式：/task <任务编号>")
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
            await event.reply(
                self._format_operation_result(
                    status="没有执行",
                    operation="切换流式传输",
                    note="已有完整本地文件，应保留可恢复上传能力",
                    task=task,
                )
            )
        elif result == "invalid":
            await event.reply(
                self._format_operation_result(
                    status="没有执行",
                    operation="切换流式传输",
                    note="当前任务状态不支持切换流式模式",
                    task=task,
                )
            )
        else:
            await event.reply(
                self._format_operation_result(
                    status="操作成功",
                    operation="切换流式传输",
                    note="已重新排队；中断后通常需要从头传输",
                    task=task,
                )
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
                "使用说明\n\n"
                "只读巡检：/orphans\n"
                "准备清理：/orphans clean\n"
                "确认清理：按 Bot 返回的一次性确认码操作"
            )
            return
        if len(parts) == 3:
            await self._confirm_orphan_cleanup(event, parts[2])
            return
        try:
            orphans = await self._find_orphan_staging()
        except NotImplementedError:
            await event.reply("临时巡检\n\n巡检结果：当前目的端不支持巡检")
            return
        except Exception as exc:  # noqa: BLE001 - external diagnostic boundary
            self.log.warning("远端临时文件巡检失败：%s", exc)
            await event.reply(
                "临时巡检\n\n巡检结果：远端巡检失败\n"
                "安全处理：未执行任何删除操作"
            )
            return
        if not orphans:
            await event.reply(
                "临时巡检\n\n"
                "巡检结果：没有发现疑似遗留文件\n"
                "安全处理：本次只读检查，没有删除内容"
            )
            return
        shown = "、".join(f"#{task_id}" for task_id, _ in orphans[:20])
        suffix = "（仅显示前 20 项）" if len(orphans) > 20 else ""
        if len(parts) == 1:
            await event.reply(
                "临时巡检\n\n"
                f"巡检结果：发现 {len(orphans)} 个疑似遗留文件\n"
                f"任务编号：{shown}{suffix}\n"
                "安全处理：本次没有删除任何内容\n"
                "清理方式：发送 /orphans clean 获取一次性确认码"
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
            "清理确认\n\n"
            f"待清数量：{len(planned)} 个疑似遗留文件{limited}\n"
            "有效时间：5 分钟\n"
            f"确认命令：/orphans clean {token}\n"
            "安全说明：确认时重新扫描，活动任务文件不会删除"
        )

    async def _confirm_orphan_cleanup(
        self, event: events.NewMessage.Event, token: str
    ) -> None:
        plan = getattr(self, "_orphan_cleanup_plan", None)
        if not plan or time.monotonic() > float(plan["expires_at"]):
            self._orphan_cleanup_plan = None
            await event.reply(
                "清理结果\n\n执行状态：没有执行\n"
                "失败原因：确认码不存在或已过期\n"
                "后续操作：请重新发送 /orphans clean"
            )
            return
        if not secrets.compare_digest(str(plan["token"]), token):
            await event.reply(
                "清理结果\n\n执行状态：没有执行\n"
                "失败原因：清理确认码不正确\n"
                "安全处理：未删除任何内容"
            )
            return
        # Consume before any mutation so retries cannot replay a partially used plan.
        self._orphan_cleanup_plan = None
        try:
            current = set(await self._find_orphan_staging())
        except Exception as exc:  # noqa: BLE001 - fail closed on rescan
            self.log.warning("清理前重新巡检远端临时文件失败：%s", exc)
            await event.reply(
                "清理结果\n\n执行状态：清理中止\n"
                "失败原因：清理前重新巡检失败\n"
                "安全处理：未删除任何内容，请重新发起巡检"
            )
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
                    "清理结果\n\n"
                    f"执行状态：清理在删除 {deleted} 个后停止\n"
                    "失败原因：无法确认下一项已经安全删除\n"
                    f"跳过数量：{protected} 个已消失或受任务保护\n"
                    "后续操作：请重新执行 /orphans 巡检"
                )
                return
            deleted += 1
        await event.reply(
            "清理结果\n\n"
            "执行状态：清理完成\n"
            f"删除数量：已删除并复查 {deleted} 个遗留临时文件\n"
            f"跳过数量：跳过 {protected} 个已消失或受任务保护的文件"
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
                self._format_operation_result(
                    status="操作成功",
                    operation="批量重试失败任务",
                    note=f"已重新排队 {count} 个；取消清理中的任务不会参与",
                )
            )
            return
        task_id = self._parse_task_id(parts)
        task = self.db.get(task_id) if task_id else None
        if task is None:
            await event.reply("用法：/retry <任务编号>")
            return
        if self._retry_task(task):
            updated = self.db.get(task_id)
            await event.reply(
                self._format_operation_result(
                    status="操作成功",
                    operation="重新加入队列",
                    note="满足目的端与资源安全条件后自动开始",
                    task=updated,
                )
            )
        else:
            await event.reply(
                self._format_operation_result(
                    status="没有执行",
                    operation="重新加入队列",
                    note="当前任务状态不需要手动重试",
                    task=task,
                )
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
            await event.reply("使用说明\n\n正确格式：/cancel <任务编号>")
            return
        if task["state"] in {
            "completed",
            "confirmed",
            "cleanup_pending",
            "cancelled",
        }:
            await event.reply(
                self._format_operation_result(
                    status="没有执行",
                    operation="取消任务",
                    note="当前任务已经结束或正在执行最终清理",
                    task=task,
                )
            )
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
            await event.reply(
                "操作结果\n\n执行状态：已经中止\n"
                "执行操作：取消任务\n后续说明：任务记录已经不存在"
            )
            return
        if task["state"] in {
            "completed",
            "confirmed",
            "cleanup_pending",
            "cancelled",
        }:
            await event.reply(
                self._format_operation_result(
                    status="没有执行",
                    operation="取消任务",
                    note="任务已在等待期间结束或进入最终清理",
                    task=task,
                )
            )
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
                    self._format_operation_result(
                    status="取消中止",
                        operation="取消任务",
                        note=f"远端无法确认安全清理，本地副本已保留：{exc}",
                        task=task,
                    )
                )
                return
        local_path = Path(task["local_path"]) if task.get("local_path") else None
        if local_path and local_path.exists():
            try:
                local_path.unlink()
            except OSError as exc:
                await event.reply(
                    self._format_operation_result(
                        status="取消中止",
                        operation="取消任务",
                        note=f"无法安全删除本地文件：{exc}",
                        task=task,
                    )
                )
                return
        part = self.settings.download_dir / f"{task_id}.part"
        if part.exists():
            try:
                part.unlink()
            except OSError as exc:
                await event.reply(
                    self._format_operation_result(
                        status="取消中止",
                        operation="取消任务",
                        note=f"无法安全删除下载临时文件：{exc}",
                        task=task,
                    )
                )
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
        updated = self.db.get(task_id)
        await event.reply(
            self._format_operation_result(
                status="操作成功",
                operation="取消任务",
                note="本地临时文件和本任务远端文件已经清理",
                task=updated,
            )
        )

    def _format_queue_task(self, task: dict[str, Any]) -> str:
        label = state_label(task["state"])
        if task.get("transfer_mode") == "stream" and task["state"] in FAILED_STATES:
            label = "流式传输失败"
        text = (
            f"任务编号：#{task['id']}｜{label}｜{format_bytes(task['file_size'])}\n"
            f"文件名称：{truncate_display(str(task['file_name']))}"
        )
        progress = getattr(self, "_progress", {}).get(int(task["id"]))
        if progress and task["state"] in {"downloading", "uploading", "streaming"}:
            age = time.monotonic() - progress["at"]
            speed = progress["speed"] if age <= 20 else 0
            percent = min(100, 100 * progress["bytes"] / max(1, task["file_size"]))
            text += f"\n传输进度：{percent:.1f}%｜{format_rate(speed)}"
        elif task.get("wait_reason"):
            text += f"\n等待原因：{truncate_display(str(task['wait_reason']))}"
        return text

    def _format_task(self, task: dict[str, Any], verbose: bool = False) -> str:
        label = state_label(task["state"])
        if task.get("transfer_mode") == "stream" and task["state"] in FAILED_STATES:
            label = "流式传输失败；无完整本地副本，远端路径记录已保留"
        text = (
            "任务详情\n\n"
            f"任务编号：#{task['id']}\n"
            f"文件名称：{task['file_name']}\n"
            f"文件大小：{format_bytes(task['file_size'])}\n"
            f"当前状态：{label}"
        )
        reason = task.get("wait_reason")
        error = task.get("error")
        if reason:
            text += f"\n等待原因：{reason}"
        if error:
            text += f"\n失败原因：{str(error)[:500]}"
        text += (
            "\n传输模式：流式传输（不占用本地任务额度）"
            if task.get("transfer_mode") == "stream"
            else "\n传输模式：普通落盘"
        )
        progress = getattr(self, "_progress", {}).get(int(task["id"]))
        if progress and task["state"] in {"downloading", "uploading", "streaming"}:
            age = time.monotonic() - progress["at"]
            speed = progress["speed"] if age <= 20 else 0
            percent = min(100, 100 * progress["bytes"] / max(1, task["file_size"]))
            text += (
                f"\n传输进度：{percent:.1f}%｜{format_rate(speed)}"
                f"\n已经耗时：{int(time.monotonic() - progress['started'])} 秒"
            )
        if verbose:
            text += (
                f"\n已经下载：{format_bytes(task['downloaded_bytes'])}"
                f"\n重试次数：下载 {task['download_retries']}，上传 {task['upload_retries']}"
            )
            if task.get("remote_path"):
                text += f"\n远端路径：{task['remote_path']}"
            if hasattr(self.db, "events"):
                history = self.db.events(int(task["id"]), limit=4)
                if history:
                    text += "\n状态记录：" + " → ".join(
                        state_label(row["new_state"])
                        for row in reversed(history)
                    )
        return text

    def _format_status(self) -> str:
        counts = self.db.counts()
        counts_text = format_status_counts(counts)
        used = self.db.used_local_bytes()
        snapshot = self.snapshot
        disk_text = "等待资源采样"
        resource_text = "等待资源采样"
        network_text = "等待资源采样"
        if snapshot:
            disk_text = format_bytes(snapshot.disk_free)
            resource_text = (
                f"CPU {snapshot.cpu_percent:.1f}%，"
                f"内存 {format_bytes(snapshot.memory_available)}"
            )
            network_text = f"{format_bytes(snapshot.network_bytes_per_second)}/s"
        checked_age = (
            f"{int(max(0, time.time() - self.destination_last_checked))} 秒前"
            if self.destination_last_checked
            else "尚未检查"
        )
        if self._destination_ready():
            scope = getattr(self, "destination_scope", "unknown")
            if scope == "target":
                destination_text = "目标目录可访问"
            elif scope == "root_fallback":
                destination_text = "根目录可访问，目标目录未创建"
            elif scope == "root":
                destination_text = "WebDAV 根目录可访问"
            else:
                destination_text = "目录可访问，范围未知"
        else:
            destination_text = f"不可用、检查过期或配置未完成（{checked_age}）"
        return (
            "系统状态\n\n"
            f"目的状态：{destination_text}\n"
            f"队列调度：{'已暂停' if self.db.is_paused() else '运行中'}\n"
            f"当前传输：下载/流式 {len(self.download_tasks)}，上传 {len(self.upload_tasks)}\n"
            f"并发窗口：下载 {self.download_window.value}，上传 {self.upload_window.value}\n"
            f"本地额度：已用 {format_bytes(used)} / {format_bytes(self.settings.local_budget_bytes)}\n"
            f"磁盘可用：{disk_text}\n"
            f"资源使用：{resource_text}\n"
            f"网络总速：{network_text}\n"
            f"来源下载：Telegram {format_rate(self._stage_rate('download'))}\n"
            f"远端上传：WebDAV {format_rate(self._stage_rate('upload'))}\n"
            f"流式送入：{format_rate(self._stage_rate('stream'))}\n"
            f"任务统计：{counts_text}"
        )

    def _format_doctor(self) -> str:
        checked = self.destination_last_checked
        age = f"{max(0, int(time.time() - checked))} 秒前" if checked else "尚未完成"
        return (
            "运行诊断\n\n"
            f"资源采样：{'正常' if self._sample_fresh() else '过期或未就绪，停止放行'}\n"
            f"检查时间：{age}\n"
            f"目的目录：{'可以访问（只读检查）' if self._destination_ready() else '未就绪或检查已过期'}\n"
            f"检查说明：{self.destination_error or '目录探测通过，不代表可写'}\n"
            f"并发窗口：下载 {self.download_window.value}，上传 {self.upload_window.value}\n"
            "写入验收：请主动执行 manage.sh verify\n"
            "敏感信息：不会在诊断结果中显示"
        )
