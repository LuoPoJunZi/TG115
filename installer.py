from __future__ import annotations

import base64
import hashlib
import ipaddress
import math
import os
import re
import select
import shlex
import socket
import socketserver
import sys
import tarfile
import tempfile
import threading
import time
import tkinter as tk
import uuid
import webbrowser
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from urllib.parse import urlsplit

import paramiko

from vps_resources import (
    GIB,
    StorageAdvice,
    StoragePlan,
    VpsResources,
    assess_storage_choice,
    build_probe_command,
    parse_probe_output,
    recommend_storage,
    validate_install_dir,
)

APP_TITLE = "Telegram → 115 一键部署器"
APP_VERSION = "1.6.0"
MANAGED_CD2_WEBDAV_URL = "http://clouddrive2:19798/dav"


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def fingerprint_sha256(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class UserRejectedHostKey(paramiko.SSHException):
    pass


class ConfirmHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, confirm: Callable[[str, str, str], bool], known_hosts: Path):
        self.confirm = confirm
        self.known_hosts = known_hosts

    def missing_host_key(
        self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey
    ) -> None:
        fingerprint = fingerprint_sha256(key)
        if not self.confirm(hostname, key.get_name(), fingerprint):
            raise UserRejectedHostKey("用户拒绝了首次出现的 VPS 主机密钥")
        client._host_keys.add(hostname, key.get_name(), key)
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        client.save_host_keys(str(self.known_hosts))


class RemoteSession:
    def __init__(
        self,
        values: dict[str, str],
        confirm_host_key: Callable[[str, str, str], bool],
    ):
        self.values = values
        self.confirm_host_key = confirm_host_key
        app_data = Path(os.getenv("APPDATA", Path.home()))
        self.known_hosts = app_data / "TG115-Deployer" / "known_hosts"
        self.client = paramiko.SSHClient()
        if self.known_hosts.exists():
            self.client.load_host_keys(str(self.known_hosts))
        self.client.set_missing_host_key_policy(
            ConfirmHostKeyPolicy(confirm_host_key, self.known_hosts)
        )

    def connect(self) -> None:
        kwargs: dict[str, object] = {
            "hostname": self.values["vps_host"].strip(),
            "port": int(self.values["vps_port"]),
            "username": self.values["vps_user"].strip(),
            "timeout": 15,
            "banner_timeout": 20,
            "auth_timeout": 20,
            "look_for_keys": False,
            "allow_agent": False,
        }
        if self.values["auth_method"] == "密码":
            kwargs["password"] = self.values["vps_password"]
        else:
            kwargs["key_filename"] = self.values["ssh_key_path"]
            passphrase = self.values["ssh_key_passphrase"]
            if passphrase:
                kwargs["passphrase"] = passphrase
        self.client.connect(**kwargs)
        transport = self.client.get_transport()
        if transport:
            transport.set_keepalive(30)

    def close(self) -> None:
        self.client.close()

    def run(
        self,
        command: str,
        *,
        sudo: bool = False,
        stream: Callable[[str], None] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, str]:
        transport = self.client.get_transport()
        if transport is None or not transport.is_active():
            raise RuntimeError("SSH 连接已经断开")
        channel = transport.open_session(timeout=15)
        channel.get_pty(width=180, height=40)
        actual = command
        if sudo:
            actual = f"sudo -S -p '' {command}"
        # All callers use fixed commands, validated paths, or shlex.quote.
        channel.exec_command(actual)  # nosec B601
        if sudo:
            password = self.values.get("sudo_password", "")
            channel.send((password + "\n").encode("utf-8"))
        output: list[str] = []
        started = time.monotonic()
        pending = ""
        while True:
            if timeout and time.monotonic() - started > timeout:
                channel.close()
                raise TimeoutError(f"远程命令超时：{command}")
            if channel.recv_ready():
                chunk = channel.recv(65536).decode("utf-8", errors="replace")
                output.append(chunk)
                pending += chunk
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if stream:
                        stream(line.rstrip("\r"))
            if channel.exit_status_ready() and not channel.recv_ready():
                break
            time.sleep(0.05)
        if pending and stream:
            stream(pending.rstrip("\r"))
        code = channel.recv_exit_status()
        return code, "".join(output)

    def sftp(self) -> paramiko.SFTPClient:
        return self.client.open_sftp()


class ForwardHandler(socketserver.BaseRequestHandler):
    ssh_transport: paramiko.Transport
    remote_host = "127.0.0.1"
    remote_port = 19798
    report_error: Callable[[Exception], None]

    def handle(self) -> None:
        try:
            channel = self.ssh_transport.open_channel(
                "direct-tcpip",
                (self.remote_host, self.remote_port),
                self.request.getpeername(),
                timeout=10,
            )
        except (OSError, paramiko.SSHException) as exc:
            self.report_error(exc)
            return
        if channel is None:
            self.report_error(RuntimeError("SSH 服务没有创建端口转发通道"))
            return
        try:
            while True:
                readable, _, _ = select.select([self.request, channel], [], [], 1)
                if self.request in readable:
                    data = self.request.recv(65536)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in readable:
                    data = channel.recv(65536)
                    if not data:
                        break
                    self.request.sendall(data)
        except (OSError, EOFError, ValueError, paramiko.SSHException) as exc:
            self.report_error(exc)
        finally:
            channel.close()
            self.request.close()


class TunnelServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Tunnel:
    def __init__(self, session: RemoteSession, local_port: int = 19798):
        transport = session.client.get_transport()
        if transport is None:
            raise RuntimeError("SSH 连接不可用")
        self._error_lock = threading.Lock()
        self._last_error: Exception | None = None
        self._closed = False
        handler_type = type(
            "CloudDriveForwardHandler",
            (ForwardHandler,),
            {
                "ssh_transport": transport,
                "report_error": staticmethod(self._report_error),
            },
        )
        self.session = session
        try:
            self.server = TunnelServer(("127.0.0.1", local_port), handler_type)
        except OSError as exc:
            raise RuntimeError(
                f"本机 SSH 隧道端口 127.0.0.1:{local_port} 无法使用，"
                "请关闭占用该端口的程序后重试。"
            ) from exc
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="cd2-tunnel", daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _report_error(self, error: Exception) -> None:
        with self._error_lock:
            self._last_error = error

    def _clear_error(self) -> None:
        with self._error_lock:
            self._last_error = None

    def _get_error(self) -> Exception | None:
        with self._error_lock:
            return self._last_error

    @staticmethod
    def _probe_error(error: Exception | None) -> RuntimeError:
        detail = str(error).strip() if error else "没有收到任何 HTTP 响应"
        forwarding_denied = (
            isinstance(error, paramiko.ChannelException)
            and error.code == paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        ) or "administratively prohibited" in detail.lower()
        if forwarding_denied:
            return RuntimeError(
                "VPS 的 SSH 服务禁止 TCP 端口转发。请检查 sshd 的 "
                "AllowTcpForwarding，并允许 PermitOpen 127.0.0.1:19798；"
                "部署器不会把 CloudDrive2 管理端口暴露到公网。"
            )
        return RuntimeError(
            "SSH 隧道已建立，但没有收到 CloudDrive2 管理页响应："
            f"{detail}。请关闭部署器后重试，并确认 VPS 本机的 19798 端口仍可访问。"
        )

    def start(self) -> None:
        self.thread.start()

    def probe(self, timeout: float = 10) -> None:
        """Verify the exact local URL before handing it to a browser."""
        self._clear_error()
        request = (
            "GET / HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        try:
            with socket.create_connection(
                ("127.0.0.1", self.port), timeout=timeout
            ) as connection:
                connection.settimeout(timeout)
                connection.sendall(request)
                response = connection.recv(4096)
        except OSError as exc:
            error = self._get_error() or exc
            raise self._probe_error(error) from error
        if not response.startswith(b"HTTP/"):
            error = self._get_error()
            raise self._probe_error(error) from error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.server.shutdown()
        self.server.server_close()
        self.session.close()


class InstallerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_TITLE} v{APP_VERSION}")
        self.root.geometry("960x760")
        self.root.minsize(900, 680)
        self.values: dict[str, tk.StringVar] = {}
        self.busy = False
        self.tunnel: Tunnel | None = None
        self.vps_resources: VpsResources | None = None
        self.storage_advice: StorageAdvice | None = None
        self.storage_probe_basis: tuple[str, bool] | None = None
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _var(self, name: str, default: str = "") -> tk.StringVar:
        variable = tk.StringVar(value=default)
        self.values[name] = variable
        return variable

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)

        heading = ttk.Label(
            outer,
            text="填写信息后先部署，再完成 CloudDrive2 WebDAV 写入验收",
            font=("Microsoft YaHei UI", 16, "bold"),
        )
        heading.pack(anchor=tk.W)
        ttk.Label(
            outer,
            text=(
                "部署器会检查 VPS、上传项目、安装 Docker、启动 CloudDrive2 和 Bot、"
                "执行健康检查。CloudDrive2 登录及 115 挂载由你本人完成。"
            ),
            wraplength=900,
        ).pack(anchor=tk.W, pady=(4, 10))

        notebook = ttk.Notebook(outer)
        notebook.pack(fill=tk.X)
        vps_tab = ttk.Frame(notebook, padding=12)
        tg_tab = ttk.Frame(notebook, padding=12)
        cd_tab = ttk.Frame(notebook, padding=12)
        option_tab = ttk.Frame(notebook, padding=12)
        notebook.add(vps_tab, text="1. VPS")
        notebook.add(tg_tab, text="2. Telegram")
        notebook.add(cd_tab, text="3. CloudDrive2 / 115")
        notebook.add(option_tab, text="4. 部署选项")

        self._build_vps_tab(vps_tab)
        self._build_telegram_tab(tg_tab)
        self._build_cloud_tab(cd_tab)
        self._build_options_tab(option_tab)

        actions = ttk.Frame(outer)
        actions.pack(fill=tk.X, pady=(10, 6))
        self.test_button = ttk.Button(
            actions, text="测试 SSH", command=self.test_connection
        )
        self.test_button.pack(side=tk.LEFT)
        self.deploy_button = ttk.Button(
            actions, text="一键部署基础环境", command=self.deploy
        )
        self.deploy_button.pack(side=tk.LEFT, padx=8)
        self.cloud_button = ttk.Button(
            actions, text="打开 CloudDrive2 管理页", command=self.open_clouddrive
        )
        self.cloud_button.pack(side=tk.LEFT)
        self.repair_button = ttk.Button(
            actions, text="修复 CloudDrive2 网络", command=self.repair_clouddrive
        )
        self.repair_button.pack(side=tk.LEFT, padx=(8, 0))
        self.verify_button = ttk.Button(
            actions, text="WebDAV 验收", command=self.verify
        )
        self.verify_button.pack(side=tk.LEFT, padx=8)
        self.clear_button = ttk.Button(
            actions, text="清空日志", command=lambda: self.log.delete("1.0", tk.END)
        )
        self.clear_button.pack(side=tk.RIGHT)

        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill=tk.X, pady=(0, 6))
        self.log = scrolledtext.ScrolledText(
            outer,
            height=18,
            font=("Consolas", 10),
            wrap=tk.WORD,
            state=tk.NORMAL,
        )
        self.log.pack(fill=tk.BOTH, expand=True)
        self._log("准备就绪。密码只保存在当前程序内存和 VPS 的受限配置文件中。")

    def _entry(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        name: str,
        default: str = "",
        *,
        secret: bool = False,
        width: int = 52,
    ) -> ttk.Entry:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=4)
        entry = ttk.Entry(
            parent,
            textvariable=self._var(name, default),
            width=width,
            show="●" if secret else "",
        )
        entry.grid(row=row, column=1, sticky=tk.EW, padx=(10, 0), pady=4)
        parent.columnconfigure(1, weight=1)
        return entry

    def _build_vps_tab(self, tab: ttk.Frame) -> None:
        self._entry(tab, 0, "VPS IP 或域名", "vps_host")
        self._entry(tab, 1, "SSH 端口", "vps_port", "22")
        self._entry(tab, 2, "SSH 用户名", "vps_user", "root")
        ttk.Label(tab, text="登录方式").grid(row=3, column=0, sticky=tk.W, pady=4)
        method = ttk.Combobox(
            tab,
            textvariable=self._var("auth_method", "密码"),
            values=("密码", "SSH 密钥"),
            state="readonly",
        )
        method.grid(row=3, column=1, sticky=tk.EW, padx=(10, 0), pady=4)
        self._entry(tab, 4, "VPS 登录密码", "vps_password", secret=True)
        key_entry = self._entry(tab, 5, "SSH 私钥文件", "ssh_key_path")
        ttk.Button(
            tab,
            text="选择",
            command=lambda: self._choose_key(key_entry),
        ).grid(row=5, column=2, padx=6)
        self._entry(tab, 6, "私钥口令（没有则留空）", "ssh_key_passphrase", secret=True)
        self._entry(
            tab,
            7,
            "sudo 密码（root 或免密 sudo 留空）",
            "sudo_password",
            secret=True,
        )
        ttk.Label(
            tab,
            text="首次连接会显示 VPS 主机密钥指纹，确认后才会保存并继续。",
            foreground="#555555",
        ).grid(row=8, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))

    def _build_telegram_tab(self, tab: ttk.Frame) -> None:
        self._entry(tab, 0, "Bot Token", "bot_token", secret=True)
        self._entry(tab, 1, "Telegram API ID", "telegram_api_id")
        self._entry(tab, 2, "Telegram API Hash", "telegram_api_hash", secret=True)
        self._entry(tab, 3, "你的 Telegram 数字 ID", "allowed_user_id")
        ttk.Label(
            tab,
            text=(
                "Bot Token 来自 @BotFather；API ID 和 API Hash 来自 my.telegram.org；"
                "数字 ID 用于限制只有你本人可以使用。"
            ),
            wraplength=800,
        ).grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(10, 0))

    def _build_cloud_tab(self, tab: ttk.Frame) -> None:
        self._entry(
            tab,
            0,
            "WebDAV 地址（同机安装保持默认）",
            "cd2_url",
            MANAGED_CD2_WEBDAV_URL,
        )
        self._entry(tab, 1, "WebDAV 用户名", "cd2_username")
        self._entry(tab, 2, "WebDAV 密码", "cd2_password", secret=True)
        self._entry(
            tab,
            3,
            "WebDAV 根目录后的子目录（可留空）",
            "cd2_target",
        )
        ttk.Label(
            tab,
            text=(
                "部署完成后点击“打开 CloudDrive2 管理页”，登录 CloudDrive2、添加并挂载 115，"
                "然后开启 WebDAV。如果 WebDAV 根目录已经选中目标 Telegram 文件夹，"
                "子目录必须留空；只有根目录在更上层时才填写相对路径。"
            ),
            wraplength=800,
        ).grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(10, 0))

    def _build_options_tab(self, tab: ttk.Frame) -> None:
        check = ttk.Checkbutton(
            tab,
            text="在 VPS 上安装并管理 CloudDrive2 容器",
            variable=self._var("deploy_clouddrive2", "true"),
            onvalue="true",
            offvalue="false",
        )
        check.grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=4)
        self._entry(tab, 1, "安装目录", "install_dir", "/opt/tg115")
        self._entry(tab, 2, "本地任务预算（GB）", "local_budget_gb", "20")
        self._entry(tab, 3, "磁盘最少保留（GB）", "min_free_disk_gb", "20")
        self._entry(tab, 4, "时区", "timezone", "Asia/Shanghai")
        resource_actions = ttk.Frame(tab)
        resource_actions.grid(
            row=5, column=0, columnspan=2, sticky=tk.W, pady=(10, 4)
        )
        self.resource_probe_button = ttk.Button(
            resource_actions,
            text="检测 VPS 并推荐",
            command=self.detect_resources,
        )
        self.resource_probe_button.pack(side=tk.LEFT)
        self.apply_balanced_button = ttk.Button(
            resource_actions,
            text="应用均衡值",
            command=lambda: self._apply_storage_plan("balanced"),
            state=tk.DISABLED,
        )
        self.apply_balanced_button.pack(side=tk.LEFT, padx=(8, 0))
        self.apply_stream_button = ttk.Button(
            resource_actions,
            text="应用流式优先值",
            command=lambda: self._apply_storage_plan("stream_first"),
            state=tk.DISABLED,
        )
        self.apply_stream_button.pack(side=tk.LEFT, padx=(8, 0))
        self.resource_summary = tk.StringVar(
            value="尚未检测 VPS；测试 SSH 时也会自动生成实例级建议。"
        )
        ttk.Label(
            tab,
            textvariable=self.resource_summary,
            wraplength=800,
            justify=tk.LEFT,
            foreground="#333333",
        ).grid(row=6, column=0, columnspan=2, sticky=tk.W, pady=(2, 4))
        ttk.Label(
            tab,
            text=(
                "源码默认仍为 20GB 本地预算和 20GB 磁盘安全线。检测只提供当前 VPS 的"
                "实例建议，点击应用后才会改输入框；部署前还会重新检测。"
                "单文件超过本地预算时自动使用流式模式。"
            ),
            wraplength=800,
        ).grid(row=7, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))

    def _choose_key(self, entry: ttk.Entry) -> None:
        path = filedialog.askopenfilename(
            title="选择 SSH 私钥",
            filetypes=[("SSH private key", "*"), ("All files", "*.*")],
        )
        if path:
            self.values["ssh_key_path"].set(path)
            entry.xview_moveto(1)

    def _snapshot(self) -> dict[str, str]:
        return {name: variable.get() for name, variable in self.values.items()}

    def _validate(self, values: dict[str, str]) -> None:
        self._validate_connection(values)
        required = {
            # These are user-facing labels, not embedded credentials.
            "bot_token": "Bot Token",  # nosec B105
            "telegram_api_id": "Telegram API ID",
            "telegram_api_hash": "Telegram API Hash",
            "allowed_user_id": "Telegram 数字 ID",
            "cd2_url": "CloudDrive2 WebDAV 地址",
            "cd2_username": "WebDAV 用户名",
            "cd2_password": "WebDAV 密码",  # nosec B105  # pragma: allowlist secret
        }
        for name, label in required.items():
            if not values.get(name, "").strip():
                raise ValueError(f"请填写：{label}")
        try:
            api_id = int(values["telegram_api_id"])
            user_id = int(values["allowed_user_id"])
            budget = float(values["local_budget_gb"])
            reserve = float(values["min_free_disk_gb"])
        except ValueError as exc:
            raise ValueError("端口、API ID、数字 ID 和磁盘数值必须是数字") from exc
        if api_id <= 0 or user_id <= 0:
            raise ValueError("API ID 和 Telegram 数字 ID 必须大于 0")
        if not math.isfinite(budget) or not math.isfinite(reserve):
            raise ValueError("磁盘预算与安全线必须是有限数字")
        if budget <= 0 or reserve <= 0:
            raise ValueError("磁盘预算与安全线必须大于 0")
        token = values["bot_token"].strip()
        if ":" not in token or len(token) < 25:
            raise ValueError("Bot Token 格式看起来不正确")
        api_hash = values["telegram_api_hash"].strip()
        if len(api_hash) != 32 or any(c not in "0123456789abcdefABCDEF" for c in api_hash):
            raise ValueError("Telegram API Hash 应为 32 位十六进制字符")
        url = self._effective_webdav_url(values)
        if not url.startswith(("http://", "https://")):
            raise ValueError("WebDAV 地址必须以 http:// 或 https:// 开头")
        parsed_url = urlsplit(url)
        if not parsed_url.hostname:
            raise ValueError("WebDAV 地址缺少有效主机名")
        if parsed_url.username or parsed_url.password:
            raise ValueError("WebDAV 用户名和密码应填写在专用输入框，不能放在地址中")
        try:
            _ = parsed_url.port
        except ValueError as exc:
            raise ValueError("WebDAV 地址端口格式不正确") from exc
        if parsed_url.query or parsed_url.fragment:
            raise ValueError("WebDAV 地址不能包含查询参数或片段")
        if any(ord(char) < 32 or char.isspace() for char in url):
            raise ValueError("WebDAV 地址不能包含空格或控制字符")
        if parsed_url.scheme == "http":
            host = parsed_url.hostname.lower()
            try:
                address = ipaddress.ip_address(host)
                private_http = (
                    address.is_private or address.is_loopback or address.is_link_local
                )
            except ValueError:
                private_http = (
                    "." not in host
                    or host == "localhost"
                    or host.endswith((".local", ".internal"))
                )
            if not private_http:
                raise ValueError("公网 WebDAV 地址必须使用 HTTPS，避免密码明文传输")
        target = values["cd2_target"].strip()
        if any(ord(char) < 32 for char in target) or "\\" in target:
            raise ValueError("115 目标路径不能包含控制字符或反斜杠")
        if ".." in target.split("/"):
            raise ValueError("115 目标路径不能包含 ..")
        validate_install_dir(values["install_dir"])
        if not re.fullmatch(r"[A-Za-z0-9_+./-]+", values["timezone"]):
            raise ValueError("时区格式不正确")
        if (
            values["timezone"].startswith("/")
            or any(part in {".", ".."} for part in values["timezone"].split("/"))
        ):
            raise ValueError("时区格式不正确")

    @staticmethod
    def _effective_webdav_url(values: dict[str, str]) -> str:
        if values.get("deploy_clouddrive2", "true") == "true":
            return MANAGED_CD2_WEBDAV_URL
        return values["cd2_url"].strip()

    def _validate_connection(self, values: dict[str, str]) -> None:
        for name, label in (
            ("vps_host", "VPS IP 或域名"),
            ("vps_port", "SSH 端口"),
            ("vps_user", "SSH 用户名"),
        ):
            if not values.get(name, "").strip():
                raise ValueError(f"请填写：{label}")
        if any(
            ord(char) < 32 or char.isspace()
            for char in values["vps_host"].strip()
        ):
            raise ValueError("VPS IP 或域名不能包含空格或控制字符")
        try:
            port = int(values["vps_port"])
        except ValueError as exc:
            raise ValueError("SSH 端口必须是数字") from exc
        if not (1 <= port <= 65535):
            raise ValueError("SSH 端口必须在 1–65535 之间")
        if any(char in values["vps_user"] for char in "\r\n"):
            raise ValueError("SSH 用户名格式不正确")
        if any(char in values.get("sudo_password", "") for char in "\r\n"):
            raise ValueError("sudo 密码不能包含换行符")
        if values["auth_method"] == "密码":
            if not values["vps_password"]:
                raise ValueError("密码登录方式必须填写 VPS 登录密码")
        else:
            key_path = Path(values["ssh_key_path"])
            if not key_path.is_file():
                raise ValueError("SSH 私钥文件不存在")

    def _log(self, text: str) -> None:
        def append() -> None:
            self.log.insert(tk.END, text.rstrip() + "\n")
            self.log.see(tk.END)

        if threading.current_thread() is threading.main_thread():
            append()
        else:
            self.root.after(0, append)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy

        def update() -> None:
            state = tk.DISABLED if busy else tk.NORMAL
            for button in (
                self.test_button,
                self.deploy_button,
                self.cloud_button,
                self.repair_button,
                self.verify_button,
                self.resource_probe_button,
            ):
                button.configure(state=state)
            advice = self.storage_advice
            self.apply_balanced_button.configure(
                state=(
                    tk.NORMAL
                    if not busy and advice and advice.balanced.safe
                    else tk.DISABLED
                )
            )
            self.apply_stream_button.configure(
                state=(
                    tk.NORMAL
                    if not busy and advice and advice.stream_first.safe
                    else tk.DISABLED
                )
            )
            if busy:
                self.progress.start(10)
            else:
                self.progress.stop()

        self.root.after(0, update)

    def _ask_host_key(self, host: str, key_type: str, fingerprint: str) -> bool:
        result: dict[str, bool] = {}
        done = threading.Event()

        def ask() -> None:
            result["approved"] = messagebox.askyesno(
                "确认 VPS 主机密钥",
                (
                    f"这是首次连接 {host}。\n\n"
                    f"类型：{key_type}\n"
                    f"指纹：{fingerprint}\n\n"
                    "请与 VPS 服务商控制台显示的指纹核对。确认一致后点击“是”。"
                ),
            )
            done.set()

        self.root.after(0, ask)
        done.wait()
        return result.get("approved", False)

    def _show_error(self, title: str, exc: Exception) -> None:
        self._log(f"[失败] {exc}")
        self.root.after(0, lambda: messagebox.showerror(title, str(exc)))

    def _run_worker(self, function: Callable[[], None]) -> None:
        if self.busy:
            return
        self._set_busy(True)

        def worker() -> None:
            try:
                function()
            except Exception as exc:  # noqa: BLE001 - GUI worker boundary
                self._show_error("操作失败", exc)
            finally:
                self._set_busy(False)

        threading.Thread(target=worker, daemon=True).start()

    def _new_session(self, values: dict[str, str]) -> RemoteSession:
        session = RemoteSession(values, self._ask_host_key)
        session.connect()
        return session

    @staticmethod
    def _storage_probe_basis(values: dict[str, str]) -> tuple[str, bool]:
        return (
            validate_install_dir(values["install_dir"]),
            values.get("deploy_clouddrive2", "true") == "true",
        )

    @staticmethod
    def _plan_summary(plan: StoragePlan, preferred_key: str | None) -> str:
        marker = "（推荐）" if plan.key == preferred_key else ""
        if not plan.safe:
            return f"{plan.label}{marker}：不可安全应用，{plan.reason}"
        return (
            f"{plan.label}{marker}：本地预算 {plan.budget_gb}GB，"
            f"最少保留 {plan.reserve_gb}GB"
        )

    def _resource_summary_text(
        self, resources: VpsResources, advice: StorageAdvice
    ) -> str:
        memory_total = resources.memory_total_bytes / GIB
        memory_available = resources.memory_available_bytes / GIB
        swap_total = resources.swap_total_bytes / GIB
        storage_total = resources.storage_total_bytes / GIB
        storage_available = resources.storage_available_bytes / GIB
        install_used = resources.install_used_bytes / GIB
        backups_used = resources.backups_used_bytes / GIB
        if resources.docker_same_filesystem:
            docker_storage = "与安装目录位于同一文件系统"
        else:
            detected = "Docker 已报告实际目录" if resources.docker_root_detected else "按默认目录估算"
            docker_available = resources.docker_storage_available_bytes / GIB
            docker_storage = (
                f"位于另一文件系统，可用 {docker_available:.1f}GB（{detected}）"
            )
        fuse = "可用" if resources.fuse_available else "不可用"
        return (
            f"检测结果：{resources.cpu_cores} 核 / 内存 {memory_total:.1f}GB"
            f"（可用 {memory_available:.1f}GB）/ Swap {swap_total:.1f}GB；"
            f"存储 {storage_total:.1f}GB（可用 {storage_available:.1f}GB，"
            f"{resources.filesystem_type}）；当前安装目录占用 {install_used:.1f}GB；"
            f"历史备份占用 {backups_used:.1f}GB；Docker：{docker_storage}；"
            f"FUSE：{fuse}；性能档位（仅提示）："
            f"{advice.performance_profile}。\n"
            f"{self._plan_summary(advice.balanced, advice.preferred_key)}；"
            f"{self._plan_summary(advice.stream_first, advice.preferred_key)}。"
        )

    def _publish_resource_advice(
        self,
        resources: VpsResources,
        advice: StorageAdvice,
        basis: tuple[str, bool],
    ) -> None:
        self.vps_resources = resources
        self.storage_advice = advice
        self.storage_probe_basis = basis
        summary = self._resource_summary_text(resources, advice)
        self._log(summary)

        def update() -> None:
            self.resource_summary.set(summary)
            if not self.busy:
                self.apply_balanced_button.configure(
                    state=tk.NORMAL if advice.balanced.safe else tk.DISABLED
                )
                self.apply_stream_button.configure(
                    state=tk.NORMAL if advice.stream_first.safe else tk.DISABLED
                )

        self.root.after(0, update)

    def _probe_and_recommend(
        self, session: RemoteSession, values: dict[str, str]
    ) -> tuple[VpsResources, StorageAdvice]:
        basis = self._storage_probe_basis(values)
        uid_code, uid_output = session.run("id -u", timeout=10)
        uid_lines = [line.strip() for line in uid_output.splitlines() if line.strip()]
        if uid_code != 0 or not uid_lines or not uid_lines[-1].isdigit():
            raise RuntimeError("SSH 已连接，但无法确认 VPS 用户权限")
        code, output = session.run(
            build_probe_command(basis[0]),
            sudo=uid_lines[-1] != "0",
            timeout=30,
        )
        if code != 0:
            raise RuntimeError("SSH 已连接，但无法读取 VPS CPU、内存和目标文件系统")
        try:
            resources = parse_probe_output(output)
        except ValueError as exc:
            raise RuntimeError(f"VPS 资源探测结果无效：{exc}") from exc
        advice = recommend_storage(resources, managed_clouddrive=basis[1])
        self._publish_resource_advice(resources, advice, basis)
        return resources, advice

    def _apply_storage_plan(self, key: str) -> None:
        advice = self.storage_advice
        if advice is None or self.storage_probe_basis is None:
            messagebox.showwarning("尚未检测", "请先检测 VPS 并生成实例级建议。")
            return
        current_basis = self._storage_probe_basis(self._snapshot())
        if current_basis != self.storage_probe_basis:
            messagebox.showwarning(
                "建议已经过期",
                "安装目录或 CloudDrive2 部署方式已经变化，请重新检测 VPS。",
            )
            return
        plan = advice.balanced if key == "balanced" else advice.stream_first
        if not plan.safe:
            messagebox.showwarning("建议不可用", plan.reason)
            return
        self.values["local_budget_gb"].set(str(plan.budget_gb))
        self.values["min_free_disk_gb"].set(str(plan.reserve_gb))
        self._log(
            f"已应用{plan.label}：本地任务预算 {plan.budget_gb}GB，"
            f"磁盘最少保留 {plan.reserve_gb}GB。"
        )

    def detect_resources(self) -> None:
        values = self._snapshot()

        def action() -> None:
            self._validate_connection(values)
            validate_install_dir(values["install_dir"])
            self._log("正在读取 VPS CPU、内存和目标文件系统……")
            session = self._new_session(values)
            try:
                self._probe_and_recommend(session, values)
            finally:
                session.close()

        self._run_worker(action)

    def test_connection(self) -> None:
        values = self._snapshot()

        def action() -> None:
            self._validate_connection(values)
            self._log("正在连接 VPS……")
            session = self._new_session(values)
            try:
                code, output = session.run(
                    "printf 'SSH_OK\\n'; uname -a; id; "
                    "awk -F= '/^(ID|VERSION_ID)=/ {print}' /etc/os-release; "
                    "free -h; df -h /"
                )
                if code != 0 or "SSH_OK" not in output:
                    raise RuntimeError("SSH 已连接，但预检命令失败")
                for line in output.splitlines():
                    self._log(line)
                self._probe_and_recommend(session, values)
                self._log("SSH 测试成功。")
                self.root.after(
                    0, lambda: messagebox.showinfo("测试成功", "SSH 连接和基础预检正常。")
                )
            finally:
                session.close()

        self._run_worker(action)

    def _build_config(self, values: dict[str, str]) -> str:
        pairs = {
            "TELEGRAM_API_ID": values["telegram_api_id"].strip(),
            "TELEGRAM_API_HASH_B64": b64(values["telegram_api_hash"].strip()),
            "BOT_TOKEN_B64": b64(values["bot_token"].strip()),
            "ALLOWED_USER_ID": values["allowed_user_id"].strip(),
            "CD2_WEBDAV_URL_B64": b64(self._effective_webdav_url(values)),
            "CD2_WEBDAV_USERNAME_B64": b64(values["cd2_username"].strip()),
            "CD2_WEBDAV_PASSWORD_B64": b64(values["cd2_password"]),
            "CD2_TARGET_PATH_B64": b64(values["cd2_target"].strip().strip("/")),
            "LOCAL_TEMP_BUDGET_GB": values["local_budget_gb"].strip(),
            "MIN_FREE_DISK_GB": values["min_free_disk_gb"].strip(),
            "CONTROL_INTERVAL_SECONDS": "3",
            "CPU_TARGET_LOW_PERCENT": "60",
            "CPU_TARGET_HIGH_PERCENT": "80",
            "CPU_PRESSURE_PERCENT": "90",
            "MEMORY_SOFT_MIN_MB": "1024",
            "MEMORY_HARD_MIN_MB": "512",
            "RAMP_UP_STEP": "1",
            "RAMP_DOWN_FACTOR": "0.5",
            "MAX_RETRIES": "3",
            "REMOTE_HEALTH_INTERVAL_SECONDS": "30",
            "TZ": values["timezone"].strip() or "Asia/Shanghai",
            "DEPLOY_CLOUDDRIVE2": values["deploy_clouddrive2"],
        }
        return "\n".join(f"{key}={value}" for key, value in pairs.items()) + "\n"

    def deploy(self) -> None:
        values = self._snapshot()

        def action() -> None:
            self._validate(values)
            payload = resource_path("payload")
            if not (payload / "remote_install.sh").is_file():
                raise RuntimeError("部署器内部 payload 缺失，请重新下载完整安装包")
            if (
                values.get("deploy_clouddrive2", "true") == "true"
                and values["cd2_url"].strip() != MANAGED_CD2_WEBDAV_URL
            ):
                self._log(
                    "CloudDrive2 与 Bot 位于同一台 VPS：已自动使用安全的容器内网 "
                    f"WebDAV 地址 {MANAGED_CD2_WEBDAV_URL}。"
                )
            self._log("开始一键部署。整个过程可能需要 5–15 分钟。")
            with tempfile.TemporaryDirectory(prefix="tg115-deployer-") as temp_name:
                temp = Path(temp_name)
                archive = temp / "payload.tar.gz"
                config_file = temp / "config.env"
                # The file is sourced by Bash on the VPS. Writing bytes avoids
                # Windows CRLF endings turning "true" into "true\r".
                config_file.write_bytes(
                    self._build_config(values).encode("utf-8")
                )
                with tarfile.open(archive, "w:gz") as tar:
                    tar.add(payload, arcname="payload")
                session = self._new_session(values)
                # UUID plus mkdir mode 700 prevents cross-user stage reuse.
                remote_stage = f"/tmp/tg115-deploy-{uuid.uuid4().hex}"  # nosec B108
                try:
                    self._log("SSH 连接成功，重新核对 VPS 资源和当前存储配置……")
                    resources, advice = self._probe_and_recommend(session, values)
                    if resources.architecture not in {
                        "x86_64", "amd64", "aarch64", "arm64"
                    }:
                        raise RuntimeError(
                            f"当前 CPU 架构暂不支持：{resources.architecture}"
                        )
                    assessment = assess_storage_choice(
                        resources,
                        budget_gb=float(values["local_budget_gb"]),
                        reserve_gb=float(values["min_free_disk_gb"]),
                        managed_clouddrive=(
                            values.get("deploy_clouddrive2", "true") == "true"
                        ),
                    )
                    if not assessment.safe:
                        preferred = advice.preferred
                        hint = (
                            f"建议先应用{preferred.label}的 "
                            f"{preferred.budget_gb}/{preferred.reserve_gb}GB。"
                            if preferred else "当前 VPS 需要释放空间或扩容后再部署。"
                        )
                        raise RuntimeError(
                            f"部署前资源校验未通过：{assessment.reason}。{hint}"
                        )
                    if (
                        advice.preferred
                        and float(values["min_free_disk_gb"])
                        < advice.preferred.reserve_gb
                    ):
                        self._log(
                            "警告：当前磁盘保留线低于实例建议；部署会继续，但应关注 "
                            "CloudDrive2 缓存和 Docker 空间。"
                        )
                    self._log(
                        f"部署前容量校验通过：预计至少需要 "
                        f"{assessment.required_available_gb:.1f}GB 可用空间。"
                    )
                    self._log("SSH 连接成功，上传部署包……")
                    code, _ = session.run(f"mkdir -m 700 {remote_stage}")
                    if code != 0:
                        raise RuntimeError("无法在 VPS 创建临时部署目录")
                    sftp = session.sftp()
                    try:
                        sftp.put(str(archive), f"{remote_stage}/payload.tar.gz")
                        sftp.put(str(config_file), f"{remote_stage}/config.env")
                        sftp.chmod(f"{remote_stage}/config.env", 0o600)
                    finally:
                        sftp.close()
                    self._log("上传完成，开始安装 VPS 运行环境和服务……")
                    extract = (
                        f"tar -xzf {remote_stage}/payload.tar.gz "
                        f"-C {remote_stage}"
                    )
                    code, output = session.run(extract, stream=self._log, timeout=120)
                    if code != 0:
                        raise RuntimeError("VPS 解压部署包失败")
                    uid_code, uid_output = session.run("id -u")
                    if uid_code != 0:
                        raise RuntimeError("无法确认 VPS 用户权限")
                    use_sudo = uid_output.strip().splitlines()[-1] != "0"
                    if use_sudo and not values.get("sudo_password"):
                        sudo_code, _ = session.run("sudo -n true")
                        if sudo_code != 0:
                            raise RuntimeError(
                                "当前用户不是 root，也没有免密 sudo；请填写 sudo 密码"
                            )
                    install_dir = values["install_dir"].strip()
                    command = (
                        f"INSTALL_DIR={shlex.quote(install_dir)} bash "
                        f"{shlex.quote(remote_stage + '/payload/remote_install.sh')} "
                        f"{shlex.quote(remote_stage + '/payload')} "
                        f"{shlex.quote(remote_stage + '/config.env')}"
                    )
                    code, output = session.run(
                        command,
                        sudo=use_sudo,
                        stream=self._log,
                        timeout=1800,
                    )
                    if code != 0 or "TG115_RESULT=SUCCESS" not in output:
                        raise RuntimeError("远程基础安装没有通过容器健康检查")
                    self._log("基础部署和容器自检通过。")
                    self.root.after(
                        0,
                        lambda: messagebox.showinfo(
                            "部署成功",
                            (
                                "Bot 已经在 VPS 上运行。\n\n"
                                "下一步：点击“打开 CloudDrive2 管理页”，"
                                "登录 CloudDrive2、添加 115 并开启 WebDAV；"
                                "然后点击“WebDAV 验收（写入测试文件）”。"
                            ),
                        ),
                    )
                finally:
                    try:
                        if re_safe_remote_stage(remote_stage):
                            session.run(f"rm -rf -- {remote_stage}", timeout=60)
                    except Exception:  # noqa: BLE001 - best-effort remote cleanup
                        self._log("警告：VPS 临时部署目录未能自动清理。")
                    session.close()

        self._run_worker(action)

    def open_clouddrive(self) -> None:
        values = self._snapshot()

        def action() -> None:
            self._validate(values)
            if self.tunnel:
                try:
                    self.tunnel.probe()
                except RuntimeError as exc:
                    self._log(f"现有 SSH 隧道已失效，正在自动重建：{exc}")
                    self.tunnel.close()
                    self.tunnel = None
                else:
                    webbrowser.open(self.tunnel.url)
                    self._log(f"CloudDrive2 管理页：{self.tunnel.url}")
                    return
            session = self._new_session(values)
            tunnel: Tunnel | None = None
            try:
                code, _ = session.run(
                    "curl -fsS --max-time 10 http://127.0.0.1:19798/ >/dev/null"
                )
                if code != 0:
                    raise RuntimeError(
                        "VPS 本机的 CloudDrive2 19798 端口没有响应，请先完成部署"
                    )
                tunnel = Tunnel(session)
                tunnel.start()
                tunnel.probe()
                self.tunnel = tunnel
                url = tunnel.url
                self._log(f"SSH 安全隧道已验收：{url}")
                webbrowser.open(url)
                self.root.after(
                    0,
                    lambda: messagebox.showinfo(
                        "CloudDrive2 管理页",
                        "管理页已通过真实 HTTP 请求验收并打开。"
                        "部署器关闭时 SSH 隧道会自动关闭。",
                    ),
                )
            except Exception:
                if tunnel:
                    tunnel.close()
                else:
                    session.close()
                raise

        self._run_worker(action)

    def repair_clouddrive(self) -> None:
        values = self._snapshot()

        def action() -> None:
            self._validate_connection(values)
            validate_install_dir(values["install_dir"])
            repair_script = resource_path("payload/repair_clouddrive_network.sh")
            if not repair_script.is_file():
                raise RuntimeError("部署器内部修复脚本缺失，请重新下载完整安装包")

            session = self._new_session(values)
            # UUID plus mkdir mode 700 prevents cross-user stage reuse.
            remote_stage = f"/tmp/tg115-deploy-{uuid.uuid4().hex}"  # nosec B108
            try:
                code, _ = session.run(f"mkdir -m 700 {remote_stage}")
                if code != 0:
                    raise RuntimeError("无法在 VPS 创建临时修复目录")
                sftp = session.sftp()
                try:
                    remote_script = f"{remote_stage}/repair_clouddrive_network.sh"
                    sftp.put(str(repair_script), remote_script)
                    sftp.chmod(remote_script, 0o700)
                finally:
                    sftp.close()

                uid_code, uid_output = session.run("id -u")
                if uid_code != 0 or not uid_output.strip():
                    raise RuntimeError("无法确认 VPS 用户权限")
                use_sudo = uid_output.strip().splitlines()[-1] != "0"
                if use_sudo and not values.get("sudo_password"):
                    sudo_code, _ = session.run("sudo -n true")
                    if sudo_code != 0:
                        raise RuntimeError("需要 sudo 密码才能修复 CloudDrive2 网络")

                install_dir = values["install_dir"].strip()
                command = (
                    f"INSTALL_DIR={shlex.quote(install_dir)} bash "
                    f"{shlex.quote(remote_script)}"
                )
                self._log("开始修复 CloudDrive2 Docker 网络并执行真实 WebDAV 验收……")
                code, output = session.run(
                    command,
                    sudo=use_sudo,
                    stream=self._log,
                    timeout=240,
                )
                if code != 0 or "TG115_REPAIR=SUCCESS" not in output:
                    raise RuntimeError("CloudDrive2 网络修复或 WebDAV 验收没有通过")
                self._log("CloudDrive2 网络修复和 WebDAV 真实验收均已通过。")
                self.root.after(
                    0,
                    lambda: messagebox.showinfo(
                        "修复成功",
                        (
                            "已保留 CloudDrive2 登录和挂载数据，修复 Docker 网络，"
                            "并通过 WebDAV 写入、校验、改名和清理测试。"
                        ),
                    ),
                )
            finally:
                try:
                    if re_safe_remote_stage(remote_stage):
                        session.run(f"rm -rf -- {remote_stage}", timeout=60)
                except Exception:  # noqa: BLE001 - best-effort remote cleanup
                    self._log("警告：VPS 临时修复目录未能自动清理。")
                session.close()

        self._run_worker(action)

    def verify(self) -> None:
        values = self._snapshot()

        def action() -> None:
            self._validate_connection(values)
            validate_install_dir(values["install_dir"])
            session = self._new_session(values)
            try:
                install_dir = values["install_dir"].strip()
                uid_code, uid_output = session.run("id -u")
                if uid_code != 0 or not uid_output.strip():
                    raise RuntimeError("无法确认 VPS 用户权限")
                use_sudo = uid_output.strip().splitlines()[-1] != "0"
                if use_sudo and not values.get("sudo_password"):
                    sudo_code, _ = session.run("sudo -n true")
                    if sudo_code != 0:
                        raise RuntimeError("需要 sudo 密码才能检查服务")
                command = (
                    f"cd {shlex.quote(install_dir)} && docker compose ps && "
                    "docker inspect --format "
                    "'BOT_HEALTH={{if .State.Health}}{{.State.Health.Status}}"
                    "{{else}}{{.State.Status}}{{end}}' tg115-bot && "
                    "docker compose exec -T tg115-bot "
                    "python -m app.verify_destination && "
                    "docker compose logs --tail=40 tg115-bot"
                )
                code, output = session.run(
                    command, sudo=use_sudo, stream=self._log, timeout=120
                )
                if code != 0:
                    if "server gave HTTP response to HTTPS client" in output:
                        raise RuntimeError(
                            "WebDAV 协议不匹配：19798 端口提供 HTTP，"
                            "当前已部署配置却使用 HTTPS。请用新版部署器重新执行"
                            "“一键部署基础环境”，再做验收。"
                        )
                    if "lookup clouddrive2" in output:
                        raise RuntimeError(
                            "Bot 无法解析 CloudDrive2 Docker 服务名。请先点击"
                            "“修复 CloudDrive2 网络”，成功后再验收。"
                        )
                    raise RuntimeError("远程状态检查失败")
                if "BOT_HEALTH=healthy" not in output:
                    raise RuntimeError("Bot 当前没有通过健康检查")
                if "TG115_DESTINATION=OK" not in output:
                    raise RuntimeError(
                        "CloudDrive2 WebDAV 写入、校验、改名和清理没有通过"
                    )
                self._log(
                    "WebDAV 验收通过：测试文件已写入、校验、改名并清理。"
                )
                self.root.after(
                    0,
                    lambda: messagebox.showinfo(
                        "验收通过",
                        (
                            "Bot 容器健康；CloudDrive2 WebDAV 已通过真实测试文件的"
                            "写入、大小校验、改名和清理。\n\n"
                            "注意：这只证明 CloudDrive2 WebDAV 已接收文件；"
                            "115 官方端应以官方客户端中大小正常且可以打开为准。"
                        ),
                    ),
                )
            finally:
                session.close()

        self._run_worker(action)

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno(
            "确认退出", "部署仍在进行。现在退出可能中断操作，确定退出吗？"
        ):
            return
        if self.tunnel:
            try:
                self.tunnel.close()
            except Exception as exc:  # noqa: BLE001 - GUI shutdown boundary
                self._log(f"警告：关闭 SSH 隧道时发生错误：{exc}")
        self.root.destroy()


def re_safe_remote_stage(path: str) -> bool:
    # Only a UUID suffix accepted by this guard can be deleted recursively.
    prefix = "/tmp/tg115-deploy-"  # nosec B108
    suffix = path.removeprefix(prefix)
    return path.startswith(prefix) and len(suffix) == 32 and all(
        char in "0123456789abcdef" for char in suffix
    )


def main() -> None:
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except tk.TclError:
        pass
    InstallerApp(root)
    root.mainloop()


def packaged_self_test(result_path: Path) -> int:
    required = (
        "payload/remote_install.sh",
        "payload/manage.sh",
        "payload/backup_retention.sh",
        "payload/repair_clouddrive_network.sh",
        "payload/docker-compose.yml",
        "payload/.dockerignore",
        "payload/Dockerfile",
        "payload/requirements.txt",
        "payload/app/__init__.py",
        "payload/app/config.py",
        "payload/app/main.py",
        "payload/app/bot_commands.py",
        "payload/app/interfaces.py",
        "payload/app/states.py",
        "payload/app/deployment_check.py",
        "payload/app/backup_database.py",
        "payload/app/naming.py",
        "payload/app/db.py",
        "payload/app/resources.py",
        "payload/app/rclone_client.py",
        "payload/app/healthcheck.py",
        "payload/app/verify_destination.py",
    )
    missing = [name for name in required if not resource_path(name).is_file()]
    gui_ok = False
    try:
        test_root = tk.Tk()
        test_root.withdraw()
        try:
            InstallerApp(test_root)
            test_root.update_idletasks()
            gui_ok = True
        finally:
            test_root.destroy()
    except tk.TclError:
        pass
    succeeded = not missing and gui_ok
    lines = [
        f"app_version={APP_VERSION}",
        f"paramiko_version={paramiko.__version__}",
        f"payload_missing={','.join(missing)}",
        f"gui_runtime={'OK' if gui_ok else 'FAILED'}",
        f"result={'OK' if succeeded else 'FAILED'}",
    ]
    result_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if succeeded else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        try:
            index = sys.argv.index("--self-test-result")
            destination = Path(sys.argv[index + 1])
        except (ValueError, IndexError):
            destination = Path(tempfile.gettempdir()) / "tg115-self-test.txt"
        raise SystemExit(packaged_self_test(destination))
    main()
