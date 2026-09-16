from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import unicodedata
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telethon.tl import functions, types

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "payload"))

from app.bot_commands import BOT_MENU_COMMANDS, truncate_display
from app.db import SCHEMA_VERSION, TaskDB
from app.deployment_check import code_fingerprint, mismatched_keys
from app.healthcheck import healthy
from app.interfaces import DestinationProbe
from app.rclone_client import (
    RcloneClient,
    RcloneError,
    _read_progress_tail,
    parse_progress,
)
from app.resources import AdaptiveWindow, ResourceSnapshot
from app.states import LEGAL_TRANSITIONS, STATE_LABELS
from test_scenarios import FakeRclone, make_service, make_settings


class OptimizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self.temp.name))
        self.db = TaskDB(self.settings.data_dir / "test.db")
        self.service = make_service(self.settings, self.db)
        self.service.client = SimpleNamespace(send_message=AsyncMock())
        self.service.rclone = FakeRclone()
        self.service._notify = AsyncMock()
        self.service._stop = asyncio.Event()
        self.service.destination_healthy = True
        self.service.destination_error = ""
        self.service.download_window = AdaptiveWindow(self.settings, "download")
        self.service.upload_window = AdaptiveWindow(self.settings, "upload")
        self.service.snapshot = ResourceSnapshot(10, 2 * 1024**3, 0, 60 * 1024**3, 0, time.time())

    async def asyncTearDown(self) -> None:
        tasks = list(self.service.download_tasks.values()) + list(self.service.upload_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.db.close()
        self.temp.cleanup()

    def task(self, *, size: int = 4, local: bool = False, message_id: int = 1) -> dict:
        task, _ = self.db.create_task(chat_id=1, message_id=message_id, sender_id=2,
                                      file_name="sample.bin", file_size=size)
        if local:
            path = self.settings.download_dir / f"{task['id']}-sample.bin"
            path.write_bytes(b"data")
            self.db.update(task["id"], state="waiting_upload", local_path=str(path))
        return self.db.get(task["id"])

    async def test_group_message_from_allowed_sender_is_ignored(self) -> None:
        event = SimpleNamespace(sender_id=self.settings.allowed_user_id, is_private=False,
                                raw_text="/pause", reply=AsyncMock())
        await self.service._on_message(event)
        self.assertFalse(self.db.is_paused())
        event.reply.assert_not_awaited()

    async def test_private_commands_remain_authorized(self) -> None:
        event = SimpleNamespace(sender_id=self.settings.allowed_user_id, is_private=True,
                                raw_text="/pause", reply=AsyncMock())
        await self.service._on_message(event)
        self.assertTrue(self.db.is_paused())
        event.reply.assert_awaited_once()

    async def test_help_does_not_advertise_retired_manual_confirmation(self) -> None:
        event = SimpleNamespace(reply=AsyncMock())
        await self.service._handle_command(event, "/help")
        text = event.reply.await_args.args[0]
        self.assertNotIn("/confirm", text)
        self.assertNotIn("人工确认", text)

    async def test_help_uses_aligned_labels_without_inline_buttons(self) -> None:
        event = SimpleNamespace(reply=AsyncMock())
        await self.service._handle_command(event, "/help")
        text = event.reply.await_args.args[0]
        for line in text.splitlines():
            if "：" in line:
                self.assertEqual(len(line.split("：", 1)[0]), 4, line)
        self.assertNotIn("buttons", event.reply.await_args.kwargs)

    async def test_native_menu_uses_equal_length_descriptions(self) -> None:
        requests = []

        class FakeClient:
            async def __call__(self, request):
                requests.append(request)

        self.service.client = FakeClient()
        await self.service._register_bot_menu()

        self.assertEqual({len(description) for _, description in BOT_MENU_COMMANDS}, {6})
        self.assertEqual(
            BOT_MENU_COMMANDS,
            (
                ("status", "查看系统状态"),
                ("queue", "查看最近任务"),
                ("pause", "暂停任务调度"),
                ("resume", "恢复任务调度"),
                ("doctor", "运行系统诊断"),
                ("orphans", "检查临时文件"),
                ("help", "查看使用帮助"),
            ),
        )
        self.assertEqual(len(requests), 2)
        self.assertIsInstance(requests[0], functions.bots.SetBotCommandsRequest)
        self.assertIsInstance(requests[0].scope, types.BotCommandScopeDefault)
        self.assertEqual(
            [(command.command, command.description) for command in requests[0].commands],
            list(BOT_MENU_COMMANDS),
        )
        self.assertIsInstance(requests[1], functions.bots.SetBotMenuButtonRequest)
        self.assertIsInstance(requests[1].button, types.BotMenuButtonCommands)

    async def test_native_menu_failure_does_not_block_service(self) -> None:
        class FailingClient:
            async def __call__(self, _request):
                raise RuntimeError("simulated Telegram failure")

        self.service.client = FailingClient()
        await self.service._register_bot_menu()
        self.service.log.warning.assert_called_once()

    async def test_queue_pages_are_compact_and_navigable(self) -> None:
        for message_id in range(1, 8):
            self.task(message_id=message_id)
        first = SimpleNamespace(reply=AsyncMock())
        await self.service._handle_command(first, "/queue")
        first_text = first.reply.await_args.args[0]
        self.assertIn("当前页码：第 1 页", first_text)
        self.assertIn("#7", first_text)
        self.assertIn("#3", first_text)
        self.assertNotIn("#2｜", first_text)
        self.assertNotIn("buttons", first.reply.await_args.kwargs)

        second = SimpleNamespace(reply=AsyncMock())
        await self.service._handle_command(second, "/queue 2")
        second_text = second.reply.await_args.args[0]
        self.assertIn("当前页码：第 2 页", second_text)
        self.assertIn("#2", second_text)
        self.assertIn("#1", second_text)
        self.assertNotIn("buttons", second.reply.await_args.kwargs)

    async def test_unauthorized_callback_cannot_pause_scheduler(self) -> None:
        event = SimpleNamespace(
            sender_id=self.settings.allowed_user_id + 1,
            chat_id=self.settings.allowed_user_id,
            data=b"control:pause",
            answer=AsyncMock(),
            edit=AsyncMock(),
        )
        await self.service._on_callback(event)
        self.assertFalse(self.db.is_paused())
        event.edit.assert_not_awaited()
        event.answer.assert_awaited_once_with("无权执行这个操作。", alert=True)

    async def test_cancel_button_requires_fresh_second_confirmation(self) -> None:
        task = self.task()
        preview = SimpleNamespace(
            sender_id=self.settings.allowed_user_id,
            chat_id=self.settings.allowed_user_id,
            data=f"task:cancel:{task['id']}".encode(),
            answer=AsyncMock(),
            edit=AsyncMock(),
        )
        await self.service._on_callback(preview)
        self.assertEqual(self.db.get(task["id"])["state"], "queued")
        self.assertIn("确认取消", preview.edit.await_args.args[0])
        confirm_data = preview.edit.await_args.kwargs["buttons"][0][0].data

        confirm = SimpleNamespace(
            sender_id=self.settings.allowed_user_id,
            chat_id=self.settings.allowed_user_id,
            data=confirm_data,
            answer=AsyncMock(),
            edit=AsyncMock(),
        )
        await self.service._on_callback(confirm)
        self.assertEqual(self.db.get(task["id"])["state"], "cancelled")
        self.assertIn("操作成功", confirm.edit.await_args.args[0])

    async def test_expired_cancel_button_does_not_change_task(self) -> None:
        task = self.task()
        preview = SimpleNamespace(
            sender_id=self.settings.allowed_user_id,
            chat_id=self.settings.allowed_user_id,
            data=f"task:cancel:{task['id']}".encode(),
            answer=AsyncMock(),
            edit=AsyncMock(),
        )
        await self.service._on_callback(preview)
        confirm_data = preview.edit.await_args.kwargs["buttons"][0][0].data
        self.service._cancel_confirmations[task["id"]]["expires_at"] = 0
        expired = SimpleNamespace(
            sender_id=self.settings.allowed_user_id,
            chat_id=self.settings.allowed_user_id,
            data=confirm_data,
            answer=AsyncMock(),
            edit=AsyncMock(),
        )
        await self.service._on_callback(expired)
        self.assertEqual(self.db.get(task["id"])["state"], "queued")
        self.assertIn("已经过期", expired.edit.await_args.args[0])

    def test_display_width_truncation_limits_long_file_names(self) -> None:
        value = truncate_display("很长的中文文件名称" * 5, max_width=20)
        width = sum(
            2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
            for char in value
        )
        self.assertLessEqual(width, 20)
        self.assertTrue(value.endswith("…"))

    def test_all_structured_menu_fields_use_four_character_labels(self) -> None:
        task = self.task()
        rendered = (
            self.service._format_help(),
            self.service._queue_page(1)[0],
            self.service._format_task(task, verbose=True),
            self.service._format_status(),
            self.service._format_doctor(),
            self.service._format_operation_result(
                status="操作成功",
                operation="重新加入队列",
                note="等待安全条件",
                task=task,
            ),
        )
        for text in rendered:
            for line in text.splitlines():
                if "：" in line:
                    self.assertEqual(len(line.split("：", 1)[0]), 4, line)

    async def test_pause_survives_restart_and_prevents_new_transfers(self) -> None:
        self.task()
        self.db.set_paused(True)
        another = TaskDB(self.db.path)
        try:
            self.assertTrue(another.is_paused())
        finally:
            another.close()
        await self.service._start_downloads()
        self.assertEqual(self.service.download_tasks, {})
        await self.service._handle_command(SimpleNamespace(reply=AsyncMock()), "/resume")
        self.assertFalse(self.db.is_paused())

    async def test_stale_resource_sample_stops_new_work(self) -> None:
        self.task()
        self.service.snapshot = replace(self.service.snapshot, sampled_at=time.time() - 100)
        await self.service._start_downloads()
        await self.service._start_uploads()
        self.assertFalse(self.service.download_tasks)
        self.assertFalse(self.service.upload_tasks)

    async def test_stale_positive_destination_sample_stops_new_work(self) -> None:
        self.task()
        self.service.destination_last_checked = time.time() - 1000
        await self.service._start_downloads()
        self.assertFalse(self.service.download_tasks)

    async def test_streaming_and_local_upload_share_window(self) -> None:
        task = self.task(size=self.settings.local_budget_bytes + 1)
        self.service.upload_tasks[99] = asyncio.create_task(asyncio.sleep(30))
        await self.service._start_downloads()
        self.assertEqual(self.db.get(task["id"])["state"], "queued")
        self.assertIn("共享上传窗口", self.db.get(task["id"])["wait_reason"])

    async def test_active_stream_prevents_extra_local_upload(self) -> None:
        stream = self.task(message_id=1)
        self.db.update(stream["id"], transfer_mode="stream", state="streaming")
        self.task(message_id=2, local=True)
        self.service.download_tasks[stream["id"]] = asyncio.create_task(asyncio.sleep(30))
        await self.service._start_uploads()
        self.assertFalse(self.service.upload_tasks)

    async def test_remote_probe_cannot_block_resource_sampling(self) -> None:
        self.service.settings = replace(self.settings, control_interval=0.01)
        entered = asyncio.Event()

        async def blocked() -> bool:
            entered.set()
            await asyncio.Event().wait()
            return True

        self.service.rclone.healthy = blocked
        samples = 0

        def sample():
            nonlocal samples
            samples += 1
            if samples >= 3:
                self.service._stop.set()
            return replace(self.service.snapshot, sampled_at=time.time())

        self.service.monitor = SimpleNamespace(sample=sample)
        probe = asyncio.create_task(self.service._destination_loop())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(self.service._resource_loop(), 1)
            self.assertGreaterEqual(samples, 3)
            self.assertTrue((self.settings.data_dir / "resource-heartbeat").exists())
        finally:
            probe.cancel()
            await asyncio.gather(probe, return_exceptions=True)

    async def test_doctor_is_read_only_and_does_not_claim_writable(self) -> None:
        text = self.service._format_doctor()
        self.assertIn("只读", text)
        self.assertIn("不代表可写", text)
        self.assertNotIn(self.settings.cd2_password, text)
        self.assertEqual(self.service.rclone.files, {})
        self.service.destination_scope = "target"
        status = self.service._format_status()
        self.assertIn("目的状态：目标目录可访问", status)
        self.assertNotIn("只读检查", status)

    async def test_cancel_before_move_cleans_both_recorded_paths(self) -> None:
        task = self.task(local=True)
        original_move = self.service.rclone.move

        async def interrupted(source, destination):
            current = self.db.get(task["id"])
            self.assertEqual(current["remote_temp_path"], source)
            self.assertEqual(current["remote_final_path"], destination)
            raise asyncio.CancelledError

        self.service.rclone.move = interrupted
        with self.assertRaises(asyncio.CancelledError):
            await self.service._upload_one(task["id"])
        self.assertTrue(Path(task["local_path"]).exists())
        self.service.rclone.move = original_move
        await self.service._command_cancel(SimpleNamespace(reply=AsyncMock()), ["/cancel", "1"])
        self.assertFalse(self.service.rclone.files)
        self.assertFalse(Path(task["local_path"]).exists())
        self.assertEqual(self.db.get(1)["state"], "cancelled")

    async def test_crash_after_move_recovers_without_reupload(self) -> None:
        task = self.task(local=True)
        original_move = self.service.rclone.move

        async def interrupted(source, destination):
            await original_move(source, destination)
            raise asyncio.CancelledError

        self.service.rclone.move = interrupted
        with self.assertRaises(asyncio.CancelledError):
            await self.service._upload_one(task["id"])
        self.db.recover(self.settings.download_dir)
        self.service.rclone.upload = AsyncMock(side_effect=AssertionError("unexpected upload"))
        await self.service._upload_one(task["id"])
        self.assertEqual(self.db.get(1)["state"], "completed")
        self.service.rclone.upload.assert_not_awaited()
        self.assertEqual(self.service.rclone.files, {"sample.bin": b"data"})

    async def test_failed_cancel_is_not_rescheduled_or_recovered(self) -> None:
        task = self.task(local=True)
        self.db.update(1, remote_temp_path=".uploading-1-sample.bin", remote_final_path="sample.bin")
        self.service.rclone.files[".uploading-1-sample.bin"] = b"data"
        self.service.rclone.remove = AsyncMock()
        await self.service._command_cancel(SimpleNamespace(reply=AsyncMock()), ["/cancel", "1"])
        self.db.recover(self.settings.download_dir)
        self.assertTrue(self.db.get(1)["cancel_requested"])
        self.assertTrue(Path(task["local_path"]).is_file())
        self.assertEqual(self.db.list_states(("waiting_upload",), ready_only=True), [])

    async def test_manual_retry_does_not_recover_other_running_tasks(self) -> None:
        first = self.task(local=True)
        second = self.task(message_id=2)
        self.db.update(first["id"], cancel_requested=1)
        self.db.update(second["id"], state="downloading")
        self.assertTrue(self.service._retry_task(self.db.get(1)))
        self.assertFalse(self.db.get(1)["cancel_requested"])
        self.assertEqual(self.db.get(2)["state"], "downloading")

    async def test_retry_cannot_race_an_in_progress_cancel(self) -> None:
        self.task(local=True)
        self.db.update(1, remote_path=".uploading-1-sample.bin")
        self.service.rclone.files[".uploading-1-sample.bin"] = b"data"
        entered, release = asyncio.Event(), asyncio.Event()
        original_remove = self.service.rclone.remove

        async def delayed(path):
            entered.set()
            await release.wait()
            await original_remove(path)

        self.service.rclone.remove = delayed
        cancel = asyncio.create_task(self.service._command_cancel(
            SimpleNamespace(reply=AsyncMock()), ["/cancel", "1"],
        ))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(self.service._retry_task(self.db.get(1)))
            await self.service._start_uploads()
            self.assertFalse(self.service.upload_tasks)
        finally:
            release.set()
            await cancel
        self.assertEqual(self.db.get(1)["state"], "cancelled")

    async def test_unknown_states_and_modes_are_rejected(self) -> None:
        self.task()
        with self.assertRaises(ValueError):
            self.db.update(1, state="typo")
        with self.assertRaises(ValueError):
            self.db.update(1, transfer_mode="typo")

    async def test_illegal_state_transition_is_rejected_atomically(self) -> None:
        self.task()
        with self.assertRaisesRegex(ValueError, "queued -> completed"):
            self.db.transition(1, "completed")
        self.assertEqual(self.db.get(1)["state"], "queued")
        self.assertEqual(self.db.events(1), [])

    async def test_transition_graph_covers_only_known_states(self) -> None:
        self.assertEqual(set(LEGAL_TRANSITIONS), set(STATE_LABELS))
        self.assertLessEqual(
            {state for targets in LEGAL_TRANSITIONS.values() for state in targets},
            set(STATE_LABELS),
        )
        source = "\n".join(
            (SOURCE / relative).read_text(encoding="utf-8")
            for relative in ("payload/app/main.py", "payload/app/bot_commands.py")
        )
        self.assertNotRegex(source, r"self\.db\.update\([\s\S]{0,80}?state=")

    async def test_manual_stream_mode_bypasses_local_budget_wait(self) -> None:
        task = self.task(size=4)
        event = SimpleNamespace(reply=AsyncMock())
        await self.service._command_stream(event, ["/stream", str(task["id"])])
        current = self.db.get(task["id"])
        self.assertEqual(current["state"], "queued")
        self.assertEqual(current["transfer_mode"], "stream")
        ok, reason = self.db.reserve(
            task["id"], budget_bytes=100, current_free_bytes=100,
            minimum_free_bytes=10,
        )
        self.assertTrue(ok)
        self.assertIn("用户选择", reason)
        self.assertEqual(self.db.get(task["id"])["transfer_mode"], "stream")

    async def test_manual_stream_still_uses_shared_upload_window(self) -> None:
        task = self.task(size=4)
        self.db.request_stream(task["id"])
        self.service.upload_tasks[99] = asyncio.create_task(asyncio.sleep(30))
        await self.service._start_downloads()
        self.assertEqual(self.db.get(task["id"])["state"], "queued")
        self.assertIn("共享上传窗口", self.db.get(task["id"])["wait_reason"])

    async def test_confirm_all_batches_completed_tasks(self) -> None:
        first = self.task(message_id=1)
        second = self.task(message_id=2)
        pending = self.task(message_id=3)
        self.db.update(first["id"], state="completed")
        self.db.update(second["id"], state="completed")
        event = SimpleNamespace(reply=AsyncMock())
        await self.service._command_confirm(event, ["/confirm", "all"])
        self.assertEqual(self.db.get(first["id"])["state"], "confirmed")
        self.assertEqual(self.db.get(second["id"])["state"], "confirmed")
        self.assertEqual(self.db.get(pending["id"])["state"], "queued")
        self.assertIn("2 个任务", event.reply.await_args.args[0])
        rendered = self.service._format_task(self.db.get(first["id"]), verbose=True)
        self.assertIn("Bot 传输已完成", rendered)
        self.assertNotIn("115 核验", rendered)

    async def test_orphan_audit_is_read_only_and_hides_file_names(self) -> None:
        tracked = self.task()
        tracked_name = f".uploading-{tracked['id']}-private-name.bin"
        self.db.update(tracked["id"], remote_temp_path=tracked_name)
        self.service.rclone.files[tracked_name] = b"data"
        self.service.rclone.files[".uploading-999-secret-title.bin"] = b"data"
        event = SimpleNamespace(reply=AsyncMock())
        await self.service._command_orphans(event)
        text = event.reply.await_args.args[0]
        self.assertIn("#999", text)
        self.assertNotIn("secret-title", text)
        self.assertIn(tracked_name, self.service.rclone.files)
        self.assertIn(".uploading-999-secret-title.bin", self.service.rclone.files)

    async def test_active_task_id_protects_unrecorded_staging_file(self) -> None:
        active = self.task()
        staging = f".uploading-{active['id']}-not-recorded-yet.bin"
        self.service.rclone.files[staging] = b"data"
        event = SimpleNamespace(reply=AsyncMock())
        await self.service._command_orphans(event)
        self.assertIn("没有发现", event.reply.await_args.args[0])
        self.assertIn(staging, self.service.rclone.files)

    async def test_orphan_cleanup_requires_one_time_confirmation(self) -> None:
        staging = ".uploading-999-private-title.bin"
        self.service.rclone.files[staging] = b"data"
        preview = SimpleNamespace(reply=AsyncMock())
        await self.service._command_orphans(preview, ["/orphans", "clean"])
        self.assertIn(staging, self.service.rclone.files)
        token = self.service._orphan_cleanup_plan["token"]

        confirm = SimpleNamespace(reply=AsyncMock())
        await self.service._command_orphans(
            confirm, ["/orphans", "clean", str(token)]
        )
        self.assertNotIn(staging, self.service.rclone.files)
        self.assertIn(staging, self.service.rclone.removed)
        self.assertIn("已删除并复查 1 个", confirm.reply.await_args.args[0])
        replay = SimpleNamespace(reply=AsyncMock())
        await self.service._command_orphans(
            replay, ["/orphans", "clean", str(token)]
        )
        self.assertIn("不存在或已过期", replay.reply.await_args.args[0])

    async def test_wrong_orphan_cleanup_token_does_not_delete(self) -> None:
        staging = ".uploading-999-private-title.bin"
        self.service.rclone.files[staging] = b"data"
        await self.service._command_orphans(
            SimpleNamespace(reply=AsyncMock()), ["/orphans", "clean"]
        )
        event = SimpleNamespace(reply=AsyncMock())
        await self.service._command_orphans(
            event, ["/orphans", "clean", "wrong-token"]
        )
        self.assertIn(staging, self.service.rclone.files)
        self.assertIn("未删除任何内容", event.reply.await_args.args[0])

    async def test_orphan_cleanup_rechecks_task_ownership(self) -> None:
        task = self.task()
        staging = f".uploading-{task['id']}-old.bin"
        self.db.update(task["id"], state="completed")
        self.service.rclone.files[staging] = b"data"
        await self.service._command_orphans(
            SimpleNamespace(reply=AsyncMock()), ["/orphans", "clean"]
        )
        token = self.service._orphan_cleanup_plan["token"]
        self.db.update(task["id"], remote_temp_path=staging)

        event = SimpleNamespace(reply=AsyncMock())
        await self.service._command_orphans(
            event, ["/orphans", "clean", str(token)]
        )
        self.assertIn(staging, self.service.rclone.files)
        self.assertEqual(self.service.rclone.removed, [])
        self.assertIn("跳过 1 个", event.reply.await_args.args[0])

    async def test_orphan_cleanup_stops_when_deletion_cannot_be_verified(self) -> None:
        staging = ".uploading-999-private-title.bin"
        self.service.rclone.files[staging] = b"data"
        self.service.rclone.remove = AsyncMock()
        await self.service._command_orphans(
            SimpleNamespace(reply=AsyncMock()), ["/orphans", "clean"]
        )
        token = self.service._orphan_cleanup_plan["token"]

        event = SimpleNamespace(reply=AsyncMock())
        await self.service._command_orphans(
            event, ["/orphans", "clean", str(token)]
        )
        self.assertIn(staging, self.service.rclone.files)
        self.assertIn("清理在删除 0 个后停止", event.reply.await_args.args[0])

    async def test_reserved_remote_names_are_not_reused(self) -> None:
        self.task()
        self.db.update(1, remote_final_path="sample.bin")
        self.assertEqual(await self.service._choose_remote_final("sample.bin", 2), "sample (task-2).bin")

    async def test_completed_state_survives_notification_cancellation(self) -> None:
        task = self.task(local=True)
        self.service._notify = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await self.service._upload_one(task["id"])
        self.assertEqual(self.db.get(1)["state"], "completed")
        self.assertFalse(Path(task["local_path"]).exists())

    async def test_changed_final_size_fails_closed_during_cleanup(self) -> None:
        task = self.task(local=True)
        self.db.update(1, state="cleanup_pending", remote_path="sample.bin", remote_final_path="sample.bin")
        self.service.rclone.files["sample.bin"] = b"wrong-size"
        await self.service._upload_one(1)
        self.assertTrue(Path(task["local_path"]).exists())
        self.assertEqual(self.db.get(1)["state"], "verification_failed_retained")
        self.assertEqual(self.db.get(1)["remote_final_path"], "sample.bin")

    async def test_retry_all_skips_unfinished_cancellations(self) -> None:
        self.task()
        self.task(message_id=2)
        self.db.update(1, state="download_failed")
        self.db.update(2, state="download_failed", cancel_requested=1)
        await self.service._command_retry(SimpleNamespace(reply=AsyncMock()), ["/retry", "all"])
        self.assertEqual(self.db.get(1)["state"], "queued")
        self.assertEqual(self.db.get(2)["state"], "download_failed")

    async def test_watch_edits_existing_message_and_stops_at_completion(self) -> None:
        self.task()
        self.db.update(1, state="completed")
        message = SimpleNamespace(raw_text="old", edit=AsyncMock())
        self.service._watch_messages[1] = message

        async def stop(_):
            self.service._stop.set()

        with patch("app.main.asyncio.sleep", side_effect=stop):
            await self.service._watch_loop()
        message.edit.assert_awaited_once()
        self.assertFalse(self.service._watch_messages)

    async def test_schema_version_and_state_history(self) -> None:
        self.task()
        self.db.update(1, state="reserved")
        self.db.update(1, downloaded_bytes=1)
        self.assertEqual(len(self.db.events(1)), 1)
        with closing(sqlite3.connect(self.db.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    async def test_newer_schema_is_rejected(self) -> None:
        path = self.settings.data_dir / "future.db"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        with self.assertRaisesRegex(RuntimeError, "数据库版本"):
            TaskDB(path)

    async def test_legacy_remote_paths_are_backfilled(self) -> None:
        self.task()
        self.db.update(1, remote_path=".uploading-1-sample.bin")
        other = TaskDB(self.db.path)
        try:
            self.assertEqual(other.get(1)["remote_temp_path"], ".uploading-1-sample.bin")
        finally:
            other.close()

    async def test_growth_probe_rolls_back_without_throughput_gain(self) -> None:
        window = self.service.download_window
        args = {"recent_errors": 0, "destination_healthy": True, "demand_present": True,
                "throughput": 100, "active_count": 2}
        with patch("app.resources.time.monotonic", return_value=0):
            self.assertEqual(window.update(self.service.snapshot, **args), 2)
        with patch("app.resources.time.monotonic", return_value=5):
            self.assertEqual(window.update(self.service.snapshot, **args), 2)
        with patch("app.resources.time.monotonic", return_value=11):
            self.assertEqual(window.update(self.service.snapshot, **args), 1)

    async def test_upload_backlog_prevents_download_window_growth(self) -> None:
        self.assertEqual(self.service.download_window.update(
            self.service.snapshot, recent_errors=0, destination_healthy=True,
            demand_present=True, throughput=100, active_count=1, backlog_pressure=True,
        ), 1)

    async def test_zero_throughput_does_not_inflate_window(self) -> None:
        for _ in range(20):
            self.service.download_window.update(
                self.service.snapshot, recent_errors=0, destination_healthy=True,
                demand_present=True, throughput=0, active_count=1,
            )
        self.assertEqual(self.service.download_window.value, 1)

    async def test_health_requires_both_fresh_heartbeats_from_current_container(self) -> None:
        root = self.settings.data_dir
        (root / "heartbeat").touch()
        now = time.time()
        self.assertFalse(healthy(root, now=now))
        (root / "resource-heartbeat").touch()
        self.assertTrue(healthy(root, now=time.time()))
        self.assertFalse(healthy(root, now=time.time(), started_at=time.time() + 1))
        os.utime(root / "resource-heartbeat", (now - 100, now - 100))
        self.assertFalse(healthy(root, now=time.time()))

    async def test_health_probe_never_creates_remote_directory(self) -> None:
        client = RcloneClient(self.settings)
        client.ensure_config = AsyncMock()
        client._run = AsyncMock(side_effect=[(3, "", ""), (0, "", "")])
        self.assertTrue(await client.healthy())
        self.assertTrue(all(call.args[0] == "lsd" for call in client._run.call_args_list))

    async def test_destination_probe_distinguishes_root_fallback(self) -> None:
        client = RcloneClient(self.settings)
        client.ensure_config = AsyncMock()
        client._run = AsyncMock(side_effect=[(3, "", ""), (0, "", "")])
        result = await client.probe()
        self.assertEqual(result, DestinationProbe(
            True, "root_fallback", "WebDAV 根目录可访问；配置的目标目录尚未创建，不代表可写",
        ))

    async def test_configured_webdav_root_is_not_reported_as_missing_target(self) -> None:
        client = RcloneClient(replace(self.settings, cd2_target=""))
        client.ensure_config = AsyncMock()
        client._run = AsyncMock(return_value=(0, "", ""))
        result = await client.probe()
        self.assertEqual(result.scope, "root")
        self.service.destination_scope = result.scope
        self.assertNotIn("尚未创建", self.service._format_status())

    async def test_staging_listing_returns_only_tg115_temp_objects(self) -> None:
        client = RcloneClient(self.settings)
        client.ensure_config = AsyncMock()
        client._run = AsyncMock(return_value=(0, json.dumps([
            {"Name": ".uploading-12-video.bin"},
            {"Name": ".uploading-13-../outside.bin"},
            {"Name": ".uploading-14-dir\\outside.bin"},
            {"Name": "normal.bin"},
            {"Name": ".uploading-invalid.bin"},
        ]), ""))
        self.assertEqual(
            await client.list_staging_objects(),
            [(12, ".uploading-12-video.bin")],
        )

    async def test_streaming_json_stats_are_parsed_incrementally_and_bounded(self) -> None:
        stream = asyncio.StreamReader()
        stream.feed_data(b"x" * 100000 + b"\n")
        stream.feed_data(json.dumps({"stats": {"bytes": 4, "speed": 2}}).encode() + b"\n")
        stream.feed_eof()
        callback = Mock()
        result = await _read_progress_tail(stream, callback)
        self.assertLessEqual(len(result), 65536)
        callback.assert_called_once_with(4, 2)
        self.assertIsNone(parse_progress(b'{"stats": {"bytes": -1}}'))
        self.assertIsNone(parse_progress(b'{"stats": {"bytes": 1, "speed": "nan"}}'))

    async def test_configuration_comparison_outputs_only_field_names(self) -> None:
        self.assertEqual(mismatched_keys({"TEST_SECRET": "new"}, {"TEST_SECRET": "old"}), ["TEST_SECRET"])
        self.assertRegex(code_fingerprint(), r"^[0-9a-f]{64}$")

    async def test_subprocess_timeout_terminates_child(self) -> None:
        client = RcloneClient(self.settings)
        create = asyncio.create_subprocess_exec
        children = []

        async def launch(*_, **options):
            child = await create(sys.executable, "-c", "import time; time.sleep(30)", **options)
            children.append(child)
            return child

        with (
            patch("app.rclone_client.asyncio.create_subprocess_exec", side_effect=launch),
            self.assertRaises(RcloneError),
        ):
            await client._run("version", timeout=0.05, progress=lambda *_: None)
        self.assertIsNotNone(children[0].returncode)

    async def test_cancelling_progress_upload_terminates_child(self) -> None:
        client = RcloneClient(self.settings)
        create = asyncio.create_subprocess_exec
        started = asyncio.Event()
        children = []

        async def launch(*_, **options):
            child = await create(sys.executable, "-c", "import time; time.sleep(30)", **options)
            children.append(child)
            started.set()
            return child

        with patch("app.rclone_client.asyncio.create_subprocess_exec", side_effect=launch):
            running = asyncio.create_task(client._run("copyto", progress=lambda *_: None))
            try:
                await asyncio.wait_for(started.wait(), 2)
            finally:
                running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(running, 3)
        self.assertIsNotNone(children[0].returncode)


if __name__ == "__main__":
    unittest.main()
