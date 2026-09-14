"""Shared persisted states, legal transitions and local-budget membership."""

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
    "completed": "Bot 传输已完成（CloudDrive2 已接收）",
    "confirmed": "115 官方端已由你确认",
    "download_failed": "下载失败",
    "upload_failed_retained": "上传失败，本地文件已保留",
    "verification_failed_retained": "校验失败，本地文件已保留",
    "cancelled": "已取消",
}

LOCAL_STATES = (
    "reserved", "downloading", "downloaded", "waiting_upload", "uploading",
    "verifying", "finalizing", "cleanup_pending", "upload_failed_retained",
    "verification_failed_retained",
)
GROWING_STATES = ("reserved", "downloading")
FAILED_STATES = ("download_failed", "upload_failed_retained", "verification_failed_retained")

# Persisted state changes must follow one of these edges. Self-transitions are
# accepted by ``can_transition`` so a retry may refresh fields without first
# inventing a synthetic state. Recovery edges are explicit because they are
# part of the supported crash-recovery contract, not unrestricted overrides.
LEGAL_TRANSITIONS = {
    "queued": {"reserved", "cancelled"},
    "reserved": {
        "downloading", "streaming", "finalizing", "cleanup_pending",
        "queued", "download_failed", "cancelled",
    },
    "downloading": {
        "downloaded", "waiting_upload", "queued", "download_failed", "cancelled",
    },
    "downloaded": {"downloading", "waiting_upload", "queued", "download_failed", "cancelled"},
    "waiting_upload": {
        "uploading", "finalizing", "cleanup_pending", "queued",
        "upload_failed_retained", "verification_failed_retained", "cancelled",
    },
    "uploading": {
        "verifying", "finalizing", "cleanup_pending", "waiting_upload", "queued",
        "upload_failed_retained", "verification_failed_retained", "cancelled",
    },
    "streaming": {
        "verifying", "finalizing", "cleanup_pending", "queued", "download_failed", "cancelled",
    },
    "verifying": {
        "uploading", "streaming", "finalizing", "cleanup_pending", "waiting_upload", "queued",
        "download_failed", "upload_failed_retained", "verification_failed_retained", "cancelled",
    },
    "finalizing": {
        "uploading", "streaming", "cleanup_pending", "waiting_upload", "queued",
        "download_failed", "upload_failed_retained", "verification_failed_retained", "cancelled",
    },
    "cleanup_pending": {
        "completed", "waiting_upload", "queued", "verification_failed_retained", "cancelled",
    },
    "download_failed": {"queued", "cancelled"},
    "upload_failed_retained": {"waiting_upload", "queued", "cancelled"},
    "verification_failed_retained": {"waiting_upload", "queued", "cancelled"},
    "completed": {"confirmed"},
    "confirmed": set(),
    "cancelled": set(),
}


def can_transition(old_state: str, new_state: str) -> bool:
    return old_state == new_state or new_state in LEGAL_TRANSITIONS.get(old_state, set())
