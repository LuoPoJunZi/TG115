from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path

from .config import Settings
from .interfaces import DestinationProbe


class RcloneError(RuntimeError):
    pass


def parse_progress(line: bytes) -> tuple[int, float] | None:
    try:
        stats = json.loads(line).get("stats", {})
        if "bytes" in stats:
            active = stats.get("transferring") or []
            item = active[0] if active else stats
            count, speed = int(item.get("bytes", stats["bytes"])), float(item.get("speed", 0))
            if count >= 0 and 0 <= speed < float("inf"):
                return count, speed
    except (ValueError, TypeError, AttributeError, KeyError, IndexError):
        pass
    return None


async def _read_progress_tail(
    stream: asyncio.StreamReader | None, progress: Callable[[int, float], None],
) -> str:
    if stream is None:
        return ""
    tail = bytearray()
    pending = bytearray()
    while chunk := await stream.read(8192):
        tail.extend(chunk)
        del tail[:-65536]
        pending.extend(chunk)
        while b"\n" in pending:
            line, _, rest = pending.partition(b"\n")
            pending = bytearray(rest)
            parsed = parse_progress(line)
            if parsed is not None:
                progress(*parsed)
        if len(pending) > 65536:
            pending.clear()
    return tail.decode("utf-8", errors="replace").strip()


async def _read_process_tail(
    stream: asyncio.StreamReader | None, limit: int = 64 * 1024
) -> str:
    if stream is None:
        return ""
    tail = bytearray()
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        tail.extend(chunk)
        if len(tail) > limit:
            del tail[:-limit]
    return tail.decode("utf-8", errors="replace").strip()


class RcloneUploadStream:
    """Async file-like sink that applies subprocess backpressure to Telethon."""

    def __init__(self, process: asyncio.subprocess.Process):
        if process.stdin is None:
            raise RuntimeError("rclone 流式进程没有标准输入")
        self.process = process
        self.stdin = process.stdin
        self.bytes_written = 0
        self._stdout_task = asyncio.create_task(
            _read_process_tail(process.stdout), name="rclone-stream-stdout"
        )
        self._stderr_task = asyncio.create_task(
            _read_process_tail(process.stderr), name="rclone-stream-stderr"
        )
        self._finished = False

    async def write(self, data: bytes) -> int:
        if self._finished:
            raise RcloneError("rclone 流式写入已经结束")
        self.stdin.write(data)
        await self.stdin.drain()
        self.bytes_written += len(data)
        return len(data)

    def tell(self) -> int:
        return self.bytes_written

    def flush(self) -> None:
        # Telethon calls flush synchronously after all awaited writes.
        return None

    async def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.stdin.close()
        try:
            await self.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass
        return_code = await self.process.wait()
        stdout, stderr = await asyncio.gather(
            self._stdout_task, self._stderr_task
        )
        if return_code != 0:
            raise RcloneError(
                f"rclone 流式上传退出码 {return_code}："
                f"{stderr or stdout or '未知错误'}"
            )

    async def abort(self) -> None:
        if self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10)
            except TimeoutError:
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
                await self.process.wait()
        self._finished = True
        await asyncio.gather(
            self._stdout_task, self._stderr_task, return_exceptions=True
        )


class RcloneClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.log = logging.getLogger("tg115.rclone")
        self._config_lock = asyncio.Lock()
        self._config_ready = False

    async def _run(
        self,
        *args: str,
        timeout: float | None = None,
        check: bool = True,
        progress: Callable[[int, float], None] | None = None,
    ) -> tuple[int, str, str]:
        command = [
            "rclone",
            "--config",
            str(self.settings.rclone_config_path),
            *args,
        ]
        self.log.debug("运行 rclone：%s", " ".join(self._redact(command)))
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        readers: list[asyncio.Task[str]] = []
        try:
            if progress is None:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
                out = stdout.decode("utf-8", errors="replace").strip()
                err = stderr.decode("utf-8", errors="replace").strip()
            else:
                readers = [asyncio.create_task(_read_process_tail(process.stdout)),
                           asyncio.create_task(_read_progress_tail(process.stderr, progress))]
                result = await asyncio.wait_for(asyncio.gather(process.wait(), *readers), timeout=timeout)
                out, err = result[1], result[2]
        except asyncio.CancelledError:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            raise
        except TimeoutError as exc:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            raise RcloneError(f"rclone 超时：{' '.join(args[:2])}") from exc
        finally:
            if readers:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
                await asyncio.gather(*readers, return_exceptions=True)
        if check and process.returncode != 0:
            raise RcloneError(
                f"rclone 退出码 {process.returncode}：{err or out or '未知错误'}"
            )
        return int(process.returncode or 0), out, err

    def _redact(self, command: list[str]) -> list[str]:
        redacted = []
        for item in command:
            if item == self.settings.cd2_password:
                redacted.append("***")
            else:
                redacted.append(item)
        return redacted

    async def ensure_config(self) -> None:
        if self._config_ready:
            return
        async with self._config_lock:
            if self._config_ready:
                return
            path = self.settings.rclone_config_path
            path.parent.mkdir(parents=True, exist_ok=True)
            common = (
                "url",
                self.settings.cd2_url,
                "vendor",
                "other",
                "user",
                self.settings.cd2_user,
                "pass",
                self.settings.cd2_password,
                "--obscure",
                "--non-interactive",
            )
            updated = False
            if path.exists() and path.stat().st_size > 0:
                code, _, _ = await self._run(
                    "config",
                    "update",
                    "cd2",
                    *common,
                    timeout=30,
                    check=False,
                )
                updated = code == 0
            if not updated:
                await self._run(
                    "config",
                    "create",
                    "cd2",
                    "webdav",
                    *common,
                    timeout=30,
                )
            try:
                os.chmod(path, 0o600)
            except OSError:
                self.log.warning("无法把 rclone 配置权限设置为 600")
            self._config_ready = True

    def remote(self, relative_path: str = "") -> str:
        target = self.settings.cd2_target.strip("/")
        relative = relative_path.strip("/")
        path = "/".join(part for part in (target, relative) if part)
        return f"cd2:{path}"

    async def prepare_destination(self) -> None:
        await self.ensure_config()
        await self._run("mkdir", self.remote(), timeout=45)
        await self._run("lsd", self.remote(), "--max-depth", "1", timeout=45)

    async def probe(self) -> DestinationProbe:
        try:
            await self.ensure_config()
            code, _, _ = await self._run(
                "lsd", self.remote(), "--max-depth", "1", timeout=20, check=False,
            )
            if code in {3, 4} and self.settings.cd2_target:
                # A not-yet-created target is prepared only by an explicit upload/verify.
                root_code, _, _ = await self._run(
                    "lsd", "cd2:", "--max-depth", "1", timeout=20, check=False,
                )
                if root_code == 0:
                    return DestinationProbe(
                        True, "root_fallback", "WebDAV 根目录可访问；配置的目标目录尚未创建，不代表可写",
                    )
                return DestinationProbe(
                    False, "root_fallback", f"WebDAV 根目录访问失败（退出码 {root_code}）"
                )
            if code != 0:
                return DestinationProbe(False, "target", f"目标目录访问失败（退出码 {code}）")
            scope = "target" if self.settings.cd2_target else "root"
            label = "目标目录" if scope == "target" else "WebDAV 根目录"
            return DestinationProbe(True, scope, f"{label}可访问；只读探测不代表可写")
        except Exception as exc:  # noqa: BLE001 - health boundary for external CLI
            self.log.warning("CloudDrive2/115 健康检查失败：%s", exc)
            return DestinationProbe(False, "unknown", "目录探测异常；请查看 VPS 最近日志")

    async def healthy(self) -> bool:
        return (await self.probe()).accessible

    async def list_staging_objects(self) -> list[tuple[int, str]]:
        """Read top-level TG115 staging objects without deleting anything."""
        await self.ensure_config()
        _, out, _ = await self._run(
            "lsjson", self.remote(), "--files-only", "--max-depth", "1", timeout=45,
        )
        try:
            payload = json.loads(out)
            if not isinstance(payload, list):
                raise TypeError("lsjson result is not a list")
            result: list[tuple[int, str]] = []
            pattern = re.compile(r"\.uploading-(\d+)-.+")
            for item in payload[:10000]:
                name = str(item.get("Name", "")) if isinstance(item, dict) else ""
                if "/" in name or "\\" in name:
                    continue
                matched = pattern.fullmatch(name)
                if matched:
                    result.append((int(matched.group(1)), name))
            return result
        except (TypeError, ValueError, AttributeError) as exc:
            raise RcloneError("无法解析远端临时文件清单") from exc

    async def upload(
        self, local_path: Path, remote_temp: str, *,
        progress: Callable[[int, float], None] | None = None,
    ) -> None:
        await self.prepare_destination()
        await self._run(
            "copyto",
            str(local_path),
            self.remote(remote_temp),
            "--transfers",
            "1",
            "--checkers",
            "2",
            "--retries",
            "2",
            "--low-level-retries",
            "5",
            "--contimeout",
            "20s",
            "--timeout",
            "5m",
            "--stats",
            "5s",
            "--stats-log-level", "NOTICE",
            "--use-json-log",
            timeout=None,
            progress=progress,
        )

    async def open_upload_stream(
        self, remote_temp: str, exact_size: int
    ) -> RcloneUploadStream:
        await self.prepare_destination()
        command = [
            "rclone",
            "--config",
            str(self.settings.rclone_config_path),
            "rcat",
            self.remote(remote_temp),
            "--size",
            str(exact_size),
            "--contimeout",
            "20s",
            "--timeout",
            "5m",
            "--retries",
            "1",
            "--low-level-retries",
            "5",
            "--stats",
            "0",
        ]
        self.log.debug("运行 rclone 流式写入：%s", " ".join(self._redact(command)))
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return RcloneUploadStream(process)

    async def remote_size(self, relative_path: str) -> int:
        _, out, _ = await self._run(
            "size", self.remote(relative_path), "--json", timeout=60
        )
        try:
            payload = json.loads(out)
            return int(payload["bytes"])
        except (ValueError, KeyError, TypeError) as exc:
            raise RcloneError(f"无法解析远端大小：{out!r}") from exc

    async def exists(self, relative_path: str) -> bool:
        code, out, err = await self._run(
            "lsjson",
            self.remote(relative_path),
            "--stat",
            timeout=45,
            check=False,
        )
        if code == 0:
            return True
        if code in {3, 4}:
            return False
        raise RcloneError(
            f"无法确认远端文件是否存在（rclone 退出码 {code}）："
            f"{err or out or '未知错误'}"
        )

    async def move(self, source: str, destination: str) -> None:
        await self._run(
            "moveto",
            self.remote(source),
            self.remote(destination),
            "--retries",
            "3",
            timeout=120,
        )

    async def remove(self, relative_path: str) -> None:
        code, out, err = await self._run(
            "deletefile", self.remote(relative_path), timeout=60, check=False
        )
        if code not in {0, 3, 4}:
            self.log.warning(
                "清理远端临时文件失败（rclone 退出码 %s）：%s",
                code,
                err or out or "未知错误",
            )
