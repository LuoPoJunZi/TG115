from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "payload"))

from app.rclone_client import RcloneClient, RcloneError
from app.verify_destination import verify_destination
from test_scenarios import make_settings


@unittest.skipUnless(shutil.which("rclone"), "需要本机 rclone；Linux CI 必须安装后运行")
class RealWebDAVTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.remote_root = root / "remote"
        self.remote_root.mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.settings = replace(make_settings(root), cd2_url=f"http://127.0.0.1:{port}", cd2_target="new-target")
        self.process = await asyncio.create_subprocess_exec(
            shutil.which("rclone"), "serve", "webdav", str(self.remote_root),
            "--config", str(root / "server.conf"), "--addr", f"127.0.0.1:{port}",
            "--user", self.settings.cd2_user, "--pass", self.settings.cd2_password,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
        )
        self.addAsyncCleanup(self.stop_server)
        self.client = RcloneClient(self.settings)
        for _ in range(100):
            if self.process.returncode is not None:
                self.fail("测试 WebDAV 服务启动失败")
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                del reader
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.05)
        else:
            self.fail("测试 WebDAV 服务未就绪")

    async def stop_server(self) -> None:
        if self.process.returncode is None:
            self.process.terminate()
            await asyncio.wait_for(self.process.wait(), 10)

    async def test_real_copy_move_stat_delete_and_root_fallback(self) -> None:
        self.assertTrue(await self.client.healthy())
        self.assertFalse((self.remote_root / "new-target").exists())
        await verify_destination(self.settings, self.client)
        self.assertEqual(list((self.remote_root / "new-target").iterdir()), [])

    async def test_real_stream_upload_and_size_verification(self) -> None:
        stream = await self.client.open_upload_stream(".uploading-stream", 4)
        try:
            await stream.write(b"data")
            await stream.finish()
        finally:
            await stream.abort()
        self.assertEqual(await self.client.remote_size(".uploading-stream"), 4)
        await self.client.move(".uploading-stream", "final.bin")
        self.assertEqual(await self.client.remote_size("final.bin"), 4)
        await self.client.remove("final.bin")
        self.assertFalse(await self.client.exists("final.bin"))

    async def test_real_upload_reports_progress(self) -> None:
        local = self.settings.data_dir / "test.bin"
        local.write_bytes(b"data" * 1024)
        progress = []
        await self.client.upload(local, ".uploading-test", progress=lambda count, speed: progress.append((count, speed)))
        self.assertTrue(progress)
        self.assertEqual(await self.client.remote_size(".uploading-test"), local.stat().st_size)
        await self.client.remove(".uploading-test")

    async def test_real_wrong_credentials_are_not_treated_as_missing(self) -> None:
        settings = replace(self.settings, cd2_password="invalid-test-only",
                           rclone_config_path=self.settings.data_dir / "wrong.conf")
        client = RcloneClient(settings)
        self.assertFalse(await client.healthy())
        with self.assertRaises(RcloneError):
            await client.exists("anything.bin")


if __name__ == "__main__":
    unittest.main()
