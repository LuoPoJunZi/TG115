from __future__ import annotations

import math
import re
import shlex
from dataclasses import dataclass, replace

GIB = 1024**3
PROBE_BEGIN = "TG115_VPS_PROBE_BEGIN"
PROBE_END = "TG115_VPS_PROBE_END"
SAFE_INSTALL_DIR = re.compile(r"/opt/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")
SUPPORTED_ARCHITECTURES = {"x86_64", "amd64", "aarch64", "arm64"}


@dataclass(frozen=True)
class VpsResources:
    architecture: str
    cpu_cores: int
    memory_total_bytes: int
    memory_available_bytes: int
    swap_total_bytes: int
    filesystem_type: str
    storage_total_bytes: int
    storage_available_bytes: int
    inodes_total: int
    inodes_available: int
    install_present: bool
    install_used_bytes: int
    downloads_used_bytes: int
    backups_used_bytes: int
    docker_same_filesystem: bool
    docker_root_detected: bool
    docker_storage_total_bytes: int
    docker_storage_available_bytes: int
    docker_inodes_total: int
    docker_inodes_available: int
    fuse_available: bool


@dataclass(frozen=True)
class StoragePlan:
    key: str
    label: str
    budget_gb: int
    reserve_gb: int
    safe: bool
    required_available_gb: float
    reason: str


@dataclass(frozen=True)
class StorageAdvice:
    performance_profile: str
    preferred_key: str | None
    balanced: StoragePlan
    stream_first: StoragePlan

    @property
    def preferred(self) -> StoragePlan | None:
        if self.preferred_key == self.balanced.key:
            return self.balanced
        if self.preferred_key == self.stream_first.key:
            return self.stream_first
        return None


@dataclass(frozen=True)
class StorageAssessment:
    safe: bool
    required_available_gb: float
    reason: str


def validate_install_dir(install_dir: str) -> str:
    value = install_dir.strip()
    if not SAFE_INSTALL_DIR.fullmatch(value):
        raise ValueError("安装目录只能是 /opt/ 下不含空格的安全路径")
    if any(part in {".", ".."} for part in value.split("/")):
        raise ValueError("安装目录不能包含 . 或 ..")
    return value


def build_probe_command(install_dir: str) -> str:
    """Build a fixed read-only probe command for the validated install path."""
    install_dir = validate_install_dir(install_dir)
    script = r'''
set -uo pipefail
export LC_ALL=C
install_dir="$1"

nearest_existing() {
  local candidate="$1" parent
  while [[ ! -e "$candidate" ]]; do
    parent="$(dirname -- "$candidate")"
    [[ "$parent" != "$candidate" ]] || break
    candidate="$parent"
  done
  [[ -e "$candidate" ]] || candidate=/
  printf '%s\n' "$candidate"
}

directory_kib() {
  local value=""
  if [[ -d "$1" ]]; then
    value="$(timeout 5s du -skx -- "$1" 2>/dev/null | awk 'NR == 1 {print $1}')" \
      || return 1
    [[ "$value" =~ ^[0-9]+$ ]] || return 1
  fi
  printf '%s\n' "${value:-0}"
}

probe_path="$(nearest_existing "$install_dir")"
docker_root=/var/lib/docker
docker_root_detected=0
if command -v docker >/dev/null 2>&1; then
  detected_root="$(timeout 5s docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
  if [[ "$detected_root" == /* && "$detected_root" != *$'\n'* && "$detected_root" != *$'\r'* ]]; then
    docker_root="$detected_root"
    docker_root_detected=1
  fi
fi
docker_path="$(nearest_existing "$docker_root")"
cpu_cores="$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || printf '1')"
memory_total_kib="$(awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo)"
memory_available_kib="$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo)"
if [[ -z "$memory_available_kib" ]]; then
  memory_available_kib="$(awk '/^MemFree:/ {print $2; exit}' /proc/meminfo)"
fi
swap_total_kib="$(awk '/^SwapTotal:/ {print $2; exit}' /proc/meminfo)"
read -r storage_total_bytes storage_available_bytes < <(
  df -PB1 -- "$probe_path" | awk 'NR == 2 {print $2, $4}'
)
read -r inodes_total inodes_available < <(
  df -Pi -- "$probe_path" | awk 'NR == 2 {print $2, $4}'
)
read -r docker_storage_total_bytes docker_storage_available_bytes < <(
  df -PB1 -- "$docker_path" | awk 'NR == 2 {print $2, $4}'
)
read -r docker_inodes_total docker_inodes_available < <(
  df -Pi -- "$docker_path" | awk 'NR == 2 {print $2, $4}'
)
filesystem_type="$(stat -f -c %T -- "$probe_path")"
install_device="$(stat -c %d -- "$probe_path")"
docker_device="$(stat -c %d -- "$docker_path")"
docker_same_filesystem=0
[[ "$install_device" == "$docker_device" ]] && docker_same_filesystem=1
install_present=0
[[ -d "$install_dir" ]] && install_present=1
fuse_available=0
[[ -c /dev/fuse ]] && fuse_available=1

printf '%s\n' 'TG115_VPS_PROBE_BEGIN'
printf 'PROBE_VERSION=2\n'
printf 'ARCHITECTURE=%s\n' "$(uname -m)"
printf 'CPU_CORES=%s\n' "$cpu_cores"
printf 'MEMORY_TOTAL_KIB=%s\n' "$memory_total_kib"
printf 'MEMORY_AVAILABLE_KIB=%s\n' "$memory_available_kib"
printf 'SWAP_TOTAL_KIB=%s\n' "${swap_total_kib:-0}"
printf 'FILESYSTEM_TYPE=%s\n' "$filesystem_type"
printf 'STORAGE_TOTAL_BYTES=%s\n' "$storage_total_bytes"
printf 'STORAGE_AVAILABLE_BYTES=%s\n' "$storage_available_bytes"
printf 'INODES_TOTAL=%s\n' "$inodes_total"
printf 'INODES_AVAILABLE=%s\n' "$inodes_available"
printf 'INSTALL_PRESENT=%s\n' "$install_present"
printf 'INSTALL_USED_KIB=%s\n' "$(directory_kib "$install_dir")"
printf 'DOWNLOADS_USED_KIB=%s\n' "$(directory_kib "$install_dir/downloads")"
printf 'BACKUPS_USED_KIB=%s\n' "$(directory_kib /opt/tg115-backups)"
printf 'DOCKER_SAME_FILESYSTEM=%s\n' "$docker_same_filesystem"
printf 'DOCKER_ROOT_DETECTED=%s\n' "$docker_root_detected"
printf 'DOCKER_STORAGE_TOTAL_BYTES=%s\n' "$docker_storage_total_bytes"
printf 'DOCKER_STORAGE_AVAILABLE_BYTES=%s\n' "$docker_storage_available_bytes"
printf 'DOCKER_INODES_TOTAL=%s\n' "$docker_inodes_total"
printf 'DOCKER_INODES_AVAILABLE=%s\n' "$docker_inodes_available"
printf 'FUSE_AVAILABLE=%s\n' "$fuse_available"
printf '%s\n' 'TG115_VPS_PROBE_END'
'''.strip()
    return f"bash -lc {shlex.quote(script)} -- {shlex.quote(install_dir)}"


def _parse_non_negative(name: str, values: dict[str, str]) -> int:
    raw = values[name]
    if not re.fullmatch(r"[0-9]+", raw):
        raise ValueError(f"VPS 资源探测字段 {name} 不是非负整数")
    return int(raw)


def parse_probe_output(output: str) -> VpsResources:
    if PROBE_BEGIN not in output or PROBE_END not in output:
        raise ValueError("VPS 资源探测结果缺少完整边界")
    block = output.rsplit(PROBE_BEGIN, 1)[1].split(PROBE_END, 1)[0]
    values: dict[str, str] = {}
    for raw_line in block.splitlines():
        line = raw_line.strip().strip("\r")
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in values:
            raise ValueError(f"VPS 资源探测字段重复：{key}")
        values[key] = value.strip()
    required = {
        "PROBE_VERSION",
        "ARCHITECTURE",
        "CPU_CORES",
        "MEMORY_TOTAL_KIB",
        "MEMORY_AVAILABLE_KIB",
        "SWAP_TOTAL_KIB",
        "FILESYSTEM_TYPE",
        "STORAGE_TOTAL_BYTES",
        "STORAGE_AVAILABLE_BYTES",
        "INODES_TOTAL",
        "INODES_AVAILABLE",
        "INSTALL_PRESENT",
        "INSTALL_USED_KIB",
        "DOWNLOADS_USED_KIB",
        "BACKUPS_USED_KIB",
        "DOCKER_SAME_FILESYSTEM",
        "DOCKER_ROOT_DETECTED",
        "DOCKER_STORAGE_TOTAL_BYTES",
        "DOCKER_STORAGE_AVAILABLE_BYTES",
        "DOCKER_INODES_TOTAL",
        "DOCKER_INODES_AVAILABLE",
        "FUSE_AVAILABLE",
    }
    missing = required - values.keys()
    if missing:
        raise ValueError("VPS 资源探测缺少字段：" + ", ".join(sorted(missing)))
    if values["PROBE_VERSION"] != "2":
        raise ValueError("VPS 资源探测版本不兼容")
    architecture = values["ARCHITECTURE"]
    filesystem_type = values["FILESYSTEM_TYPE"]
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", architecture):
        raise ValueError("VPS CPU 架构字段格式异常")
    if not re.fullmatch(r"[A-Za-z0-9._+/-]+", filesystem_type):
        raise ValueError("VPS 文件系统类型字段格式异常")
    numbers = {name: _parse_non_negative(name, values) for name in required if name not in {
        "PROBE_VERSION", "ARCHITECTURE", "FILESYSTEM_TYPE"
    }}
    cpu_cores = numbers["CPU_CORES"]
    memory_total = numbers["MEMORY_TOTAL_KIB"] * 1024
    memory_available = numbers["MEMORY_AVAILABLE_KIB"] * 1024
    storage_total = numbers["STORAGE_TOTAL_BYTES"]
    storage_available = numbers["STORAGE_AVAILABLE_BYTES"]
    docker_storage_total = numbers["DOCKER_STORAGE_TOTAL_BYTES"]
    docker_storage_available = numbers["DOCKER_STORAGE_AVAILABLE_BYTES"]
    if not 1 <= cpu_cores <= 4096:
        raise ValueError("VPS CPU 核心数超出合理范围")
    if memory_total <= 0 or memory_available > memory_total:
        raise ValueError("VPS 内存探测结果不合理")
    if storage_total <= 0 or storage_available > storage_total:
        raise ValueError("VPS 存储探测结果不合理")
    if docker_storage_total <= 0 or docker_storage_available > docker_storage_total:
        raise ValueError("VPS Docker 存储探测结果不合理")
    for flag in (
        "INSTALL_PRESENT",
        "DOCKER_SAME_FILESYSTEM",
        "DOCKER_ROOT_DETECTED",
        "FUSE_AVAILABLE",
    ):
        if numbers[flag] not in {0, 1}:
            raise ValueError(f"VPS 资源探测标志 {flag} 格式异常")
    return VpsResources(
        architecture=architecture,
        cpu_cores=cpu_cores,
        memory_total_bytes=memory_total,
        memory_available_bytes=memory_available,
        swap_total_bytes=numbers["SWAP_TOTAL_KIB"] * 1024,
        filesystem_type=filesystem_type,
        storage_total_bytes=storage_total,
        storage_available_bytes=storage_available,
        inodes_total=numbers["INODES_TOTAL"],
        inodes_available=numbers["INODES_AVAILABLE"],
        install_present=bool(numbers["INSTALL_PRESENT"]),
        install_used_bytes=numbers["INSTALL_USED_KIB"] * 1024,
        downloads_used_bytes=numbers["DOWNLOADS_USED_KIB"] * 1024,
        backups_used_bytes=numbers["BACKUPS_USED_KIB"] * 1024,
        docker_same_filesystem=bool(numbers["DOCKER_SAME_FILESYSTEM"]),
        docker_root_detected=bool(numbers["DOCKER_ROOT_DETECTED"]),
        docker_storage_total_bytes=docker_storage_total,
        docker_storage_available_bytes=docker_storage_available,
        docker_inodes_total=numbers["DOCKER_INODES_TOTAL"],
        docker_inodes_available=numbers["DOCKER_INODES_AVAILABLE"],
        fuse_available=bool(numbers["FUSE_AVAILABLE"]),
    )


def _performance_profile(resources: VpsResources) -> str:
    memory_gb = resources.memory_total_bytes / GIB
    if resources.cpu_cores >= 4 and memory_gb >= 7.5:
        return "批量"
    if resources.cpu_cores >= 2 and memory_gb >= 3.5:
        return "标准"
    return "保守"


def _installation_headroom_gb(
    resources: VpsResources, managed_clouddrive: bool
) -> int:
    if resources.install_present:
        return 3
    return 6 if managed_clouddrive else 3


def _docker_headroom_gb(
    resources: VpsResources, managed_clouddrive: bool
) -> int:
    if resources.install_present:
        return 3 if managed_clouddrive else 2
    return 6 if managed_clouddrive else 4


def _resource_blocker(
    resources: VpsResources, *, managed_clouddrive: bool
) -> str | None:
    if resources.architecture not in SUPPORTED_ARCHITECTURES:
        return f"当前 CPU 架构暂不支持：{resources.architecture}"
    if resources.memory_total_bytes < 1800 * 1024**2:
        return "VPS 总内存不足 2GB 安装门槛"
    if managed_clouddrive and not resources.fuse_available:
        return "VPS 未提供 /dev/fuse，无法运行受管 CloudDrive2"
    if resources.inodes_total and resources.inodes_available < max(
        1000, math.ceil(resources.inodes_total * 0.01)
    ):
        return "目标文件系统可用 inode 低于安全线"
    if not resources.docker_same_filesystem:
        docker_required_gb = _docker_headroom_gb(resources, managed_clouddrive)
        docker_available_gb = resources.docker_storage_available_bytes / GIB
        if docker_available_gb + 1e-9 < docker_required_gb:
            return (
                f"Docker 数据文件系统至少需要 {docker_required_gb}GB 可用空间，"
                f"当前只有 {docker_available_gb:.1f}GB"
            )
        if (
            resources.docker_inodes_total
            and resources.docker_inodes_available
            < max(1000, math.ceil(resources.docker_inodes_total * 0.01))
        ):
            return "Docker 数据文件系统可用 inode 低于安全线"
    return None


def _make_plan(
    *,
    key: str,
    label: str,
    raw_budget_gb: float,
    maximum_budget_gb: int,
    reserve_gb: int,
    available_gb: float,
    occupied_gb: float,
    overhead_gb: int,
) -> StoragePlan:
    occupied_ceiling = math.ceil(occupied_gb)
    budget_gb = max(occupied_ceiling, min(maximum_budget_gb, math.floor(raw_budget_gb)))
    additional_budget = max(0.0, budget_gb - occupied_gb)
    required = reserve_gb + overhead_gb + additional_budget
    safe = budget_gb >= 2 and available_gb + 1e-9 >= required
    if safe:
        reason = "满足任务预算、磁盘保留线和安装余量"
    elif budget_gb < 2:
        reason = "扣除磁盘保留线和安装余量后不足 2GB 本地任务预算"
    else:
        reason = f"至少需要 {required:.1f}GB 可用空间"
    return StoragePlan(
        key=key,
        label=label,
        budget_gb=budget_gb,
        reserve_gb=reserve_gb,
        safe=safe,
        required_available_gb=required,
        reason=reason,
    )


def recommend_storage(
    resources: VpsResources, *, managed_clouddrive: bool
) -> StorageAdvice:
    total_gb = resources.storage_total_bytes / GIB
    available_gb = resources.storage_available_bytes / GIB
    occupied_gb = resources.downloads_used_bytes / GIB
    overhead_gb = _installation_headroom_gb(resources, managed_clouddrive)
    reserve_gb = max(8, min(20, math.ceil(total_gb * 0.20)))
    safe_pool_gb = available_gb - overhead_gb - reserve_gb
    balanced = _make_plan(
        key="balanced",
        label="均衡模式",
        raw_budget_gb=max(0.0, safe_pool_gb) * 0.60,
        maximum_budget_gb=20,
        reserve_gb=reserve_gb,
        available_gb=available_gb,
        occupied_gb=occupied_gb,
        overhead_gb=overhead_gb,
    )
    stream_first = _make_plan(
        key="stream_first",
        label="流式优先",
        raw_budget_gb=max(0.0, safe_pool_gb) * 0.50,
        maximum_budget_gb=8,
        reserve_gb=reserve_gb,
        available_gb=available_gb,
        occupied_gb=occupied_gb,
        overhead_gb=overhead_gb,
    )
    blocker = _resource_blocker(
        resources, managed_clouddrive=managed_clouddrive
    )
    if blocker:
        balanced = replace(balanced, safe=False, reason=blocker)
        stream_first = replace(stream_first, safe=False, reason=blocker)
    if stream_first.safe and (
        total_gb < 50 or available_gb < 40 or balanced.budget_gb < 12
    ):
        preferred_key = stream_first.key
    elif balanced.safe:
        preferred_key = balanced.key
    elif stream_first.safe:
        preferred_key = stream_first.key
    else:
        preferred_key = None
    return StorageAdvice(
        performance_profile=_performance_profile(resources),
        preferred_key=preferred_key,
        balanced=balanced,
        stream_first=stream_first,
    )


def assess_storage_choice(
    resources: VpsResources,
    *,
    budget_gb: float,
    reserve_gb: float,
    managed_clouddrive: bool,
) -> StorageAssessment:
    available_gb = resources.storage_available_bytes / GIB
    occupied_gb = resources.downloads_used_bytes / GIB
    overhead_gb = _installation_headroom_gb(resources, managed_clouddrive)
    additional_budget = max(0.0, budget_gb - occupied_gb)
    required = max(8.0, reserve_gb + overhead_gb + additional_budget)
    blocker = _resource_blocker(
        resources, managed_clouddrive=managed_clouddrive
    )
    if blocker:
        return StorageAssessment(False, required, blocker)
    if available_gb + 1e-9 < required:
        return StorageAssessment(
            False,
            required,
            f"当前配置至少需要 {required:.1f}GB 可用空间，VPS 只有 {available_gb:.1f}GB",
        )
    return StorageAssessment(True, required, "当前配置通过部署前容量校验")
