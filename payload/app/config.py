from __future__ import annotations

import base64
import math
import os
from dataclasses import dataclass
from pathlib import Path


def _decode_b64(name: str, *, required: bool = True, default: str = "") -> str:
    value = os.getenv(name, "")
    if not value:
        if required:
            raise RuntimeError(f"缺少必填配置：{name}")
        return default
    try:
        return base64.b64decode(value.encode("ascii"), validate=True).decode("utf-8")
    except Exception as exc:
        raise RuntimeError(f"配置 {name} 不是有效的 Base64 UTF-8 数据") from exc


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    try:
        return int(value) if value is not None else default
    except ValueError as exc:
        raise RuntimeError(f"配置 {name} 必须是整数") from exc


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    try:
        result = float(value) if value is not None else default
        if not math.isfinite(result):
            raise ValueError("non-finite")
        return result
    except ValueError as exc:
        raise RuntimeError(f"配置 {name} 必须是有限数字") from exc


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    allowed_user_id: int
    cd2_url: str
    cd2_user: str
    cd2_password: str
    cd2_target: str
    data_dir: Path
    download_dir: Path
    log_dir: Path
    rclone_config_path: Path
    local_budget_bytes: int
    min_free_disk_bytes: int
    control_interval: float
    cpu_target_low: float
    cpu_target_high: float
    cpu_pressure: float
    memory_soft_min_bytes: int
    memory_hard_min_bytes: int
    ramp_up_step: int
    ramp_down_factor: float
    max_retries: int
    remote_health_interval: float

    @classmethod
    def from_env(cls, *, create_directories: bool = True) -> Settings:
        api_id = _int("TELEGRAM_API_ID", 0)
        allowed_user_id = _int("ALLOWED_USER_ID", 0)
        if api_id <= 0:
            raise RuntimeError("TELEGRAM_API_ID 必须大于 0")
        if allowed_user_id <= 0:
            raise RuntimeError("ALLOWED_USER_ID 必须大于 0")

        data_dir = Path(os.getenv("DATA_DIR", "/data"))
        download_dir = Path(os.getenv("DOWNLOAD_DIR", "/downloads"))
        log_dir = Path(os.getenv("LOG_DIR", "/logs"))
        rclone_config_path = Path(
            os.getenv("RCLONE_CONFIG_PATH", "/config/rclone/rclone.conf")
        )
        budget_gb = _float("LOCAL_TEMP_BUDGET_GB", 20.0)
        min_free_gb = _float("MIN_FREE_DISK_GB", 20.0)
        if (
            not math.isfinite(budget_gb)
            or not math.isfinite(min_free_gb)
            or budget_gb <= 0
            or min_free_gb <= 0
        ):
            raise RuntimeError("磁盘预算和安全线必须是大于 0 的有限数字")

        settings = cls(
            api_id=api_id,
            api_hash=_decode_b64("TELEGRAM_API_HASH_B64"),
            bot_token=_decode_b64("BOT_TOKEN_B64"),
            allowed_user_id=allowed_user_id,
            cd2_url=_decode_b64("CD2_WEBDAV_URL_B64"),
            cd2_user=_decode_b64("CD2_WEBDAV_USERNAME_B64"),
            cd2_password=_decode_b64("CD2_WEBDAV_PASSWORD_B64"),
            cd2_target=_decode_b64(
                "CD2_TARGET_PATH_B64", required=False
            ).strip("/"),
            data_dir=data_dir,
            download_dir=download_dir,
            log_dir=log_dir,
            rclone_config_path=rclone_config_path,
            local_budget_bytes=int(budget_gb * 1024**3),
            min_free_disk_bytes=int(min_free_gb * 1024**3),
            control_interval=max(1.0, _float("CONTROL_INTERVAL_SECONDS", 3.0)),
            cpu_target_low=_float("CPU_TARGET_LOW_PERCENT", 60.0),
            cpu_target_high=_float("CPU_TARGET_HIGH_PERCENT", 80.0),
            cpu_pressure=_float("CPU_PRESSURE_PERCENT", 90.0),
            memory_soft_min_bytes=_int("MEMORY_SOFT_MIN_MB", 1024) * 1024**2,
            memory_hard_min_bytes=_int("MEMORY_HARD_MIN_MB", 512) * 1024**2,
            ramp_up_step=max(1, _int("RAMP_UP_STEP", 1)),
            ramp_down_factor=min(0.9, max(0.1, _float("RAMP_DOWN_FACTOR", 0.5))),
            max_retries=max(1, _int("MAX_RETRIES", 3)),
            remote_health_interval=max(
                10.0, _float("REMOTE_HEALTH_INTERVAL_SECONDS", 30.0)
            ),
        )
        if not (
            0
            < settings.cpu_target_low
            < settings.cpu_target_high
            < settings.cpu_pressure
            <= 100
        ):
            raise RuntimeError("CPU 阈值必须满足 0 < LOW < HIGH < PRESSURE <= 100")
        if not 0 < settings.memory_hard_min_bytes <= settings.memory_soft_min_bytes:
            raise RuntimeError("内存阈值必须满足 0 < HARD <= SOFT")
        for path in (
            settings.data_dir,
            settings.download_dir,
            settings.log_dir,
            settings.rclone_config_path.parent,
        ):
            if create_directories:
                path.mkdir(parents=True, exist_ok=True)
        return settings
