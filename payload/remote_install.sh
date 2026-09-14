#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SOURCE_DIR="${1:-}"
CONFIG_FILE="${2:-}"
INSTALL_DIR="${INSTALL_DIR:-/opt/tg115}"
BACKUP=""
DATABASE_BACKUP=""
RELEASE_STAGE=""
CANDIDATE_TAG=""
OLD_IMAGE_ID=""
OLD_IMAGE_TAG=""
DB_TEMP_NAME=""
ROLLBACK_ARMED=false

PROGRAM_PATHS=(
  app .dockerignore backup_retention.sh Dockerfile docker-compose.yml manage.sh remote_install.sh
  repair_clouddrive_network.sh requirements.txt
)

log() {
  printf '[TG115] %s\n' "$*"
}

remove_program_files() {
  local relative
  for relative in "${PROGRAM_PATHS[@]}"; do
    rm -rf -- "${INSTALL_DIR:?}/$relative" || return 1
  done
}

rollback_install() {
  ROLLBACK_ARMED=false
  [[ -n "$BACKUP" && -f "$BACKUP" ]] || return 1
  log "新版本未通过验收，开始恢复升级前的程序、配置和数据库"
  if [[ -f "$INSTALL_DIR/docker-compose.yml" ]]; then
    (cd "$INSTALL_DIR" && docker compose rm -sf tg115-bot) || true
  fi
  remove_program_files || return 1
  tar -xzf "$BACKUP" -C "$INSTALL_DIR" || return 1
  if [[ -n "$DATABASE_BACKUP" && -f "$DATABASE_BACKUP" ]]; then
    rm -f -- "$INSTALL_DIR/data/tg115.db-wal" "$INSTALL_DIR/data/tg115.db-shm" || return 1
    install -m 600 "$DATABASE_BACKUP" "$INSTALL_DIR/data/tg115.db" || return 1
    chown 10001:10001 "$INSTALL_DIR/data/tg115.db" || return 1
  fi
  if [[ -n "$OLD_IMAGE_ID" && -n "$OLD_IMAGE_TAG" ]]; then
    docker tag "$OLD_IMAGE_ID" "$OLD_IMAGE_TAG" || return 1
  fi
  cd "$INSTALL_DIR" || return 1
  docker compose up -d --no-deps --force-recreate tg115-bot || return 1
  local health=""
  for _ in $(seq 1 40); do
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' tg115-bot 2>/dev/null || true)"
    [[ "$health" == healthy ]] && return 0
    [[ "$health" != unhealthy && "$health" != exited ]] || break
    sleep 3
  done
  return 1
}

cleanup_release_stage() {
  if [[ -n "$RELEASE_STAGE" && "$RELEASE_STAGE" == /opt/tg115-release-* && -d "$RELEASE_STAGE" ]]; then
    rm -rf -- "$RELEASE_STAGE"
  fi
  if [[ -n "$CANDIDATE_TAG" ]]; then
    docker image rm "$CANDIDATE_TAG" >/dev/null 2>&1 || true
  fi
  if [[ "$DB_TEMP_NAME" == .upgrade-backup-*.db ]]; then
    rm -f -- "$INSTALL_DIR/data/$DB_TEMP_NAME"
  fi
}

fail() {
  trap - ERR INT TERM HUP
  local message="$*"
  if [[ "$ROLLBACK_ARMED" == true ]]; then
    if rollback_install; then
      message+=" 已自动恢复升级前版本。"
    else
      message+=" 自动恢复未完成，请保留备份并人工处理。"
    fi
  fi
  printf '[TG115][ERROR] %s\n' "$message" >&2
  printf 'TG115_RESULT=FAILED\n' >&2
  exit 1
}

trap 'fail "安装在第 ${LINENO} 行失败。请保留完整日志。"' ERR
trap 'fail "安装被中断。请保留完整日志。"' INT TERM HUP
trap cleanup_release_stage EXIT

[[ "$(id -u)" -eq 0 ]] || fail "remote_install.sh 必须以 root 或 sudo 运行"
[[ -d "$SOURCE_DIR" ]] || fail "找不到部署源目录：$SOURCE_DIR"
[[ -f "$CONFIG_FILE" ]] || fail "找不到配置文件：$CONFIG_FILE"
[[ "$INSTALL_DIR" =~ ^/opt/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$ ]] \
  || fail "安装目录必须是 /opt/ 下的安全绝对路径"
[[ "/$INSTALL_DIR/" != *"/../"* && "/$INSTALL_DIR/" != *"/./"* ]] \
  || fail "安装目录不能包含 . 或 .. 路径段"
[[ "$(realpath -m -- "$INSTALL_DIR")" == "$INSTALL_DIR" ]] \
  || fail "安装目录不能经过符号链接"
SOURCE_DIR="$(realpath -e -- "$SOURCE_DIR")"
CONFIG_FILE="$(realpath -e -- "$CONFIG_FILE")"
[[ "$SOURCE_DIR/" != "$INSTALL_DIR/"* && "$INSTALL_DIR/" != "$SOURCE_DIR/"* ]] \
  || fail "部署源目录和安装目录不能重叠"
[[ "$CONFIG_FILE" != "$INSTALL_DIR/"* ]] || fail "请将输入配置放在安装目录之外"
export INSTALL_DIR

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  source /etc/os-release
else
  fail "无法识别 Linux 系统"
fi
case "${ID:-}" in
  ubuntu|debian) ;;
  *) fail "目前只自动支持 Ubuntu 或 Debian，当前系统：${ID:-unknown}" ;;
esac
[[ -n "${VERSION_CODENAME:-}" ]] || fail "系统缺少 VERSION_CODENAME，无法配置软件源"

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64|aarch64|arm64) ;;
  *) fail "当前 CPU 架构暂不支持：$ARCH" ;;
esac

TOTAL_MEM_MB="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
CPU_CORES="$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc)"
INSTALL_PROBE_PATH="$INSTALL_DIR"
while [[ ! -e "$INSTALL_PROBE_PATH" ]]; do
  PARENT_PATH="$(dirname -- "$INSTALL_PROBE_PATH")"
  [[ "$PARENT_PATH" != "$INSTALL_PROBE_PATH" ]] || break
  INSTALL_PROBE_PATH="$PARENT_PATH"
done
[[ -e "$INSTALL_PROBE_PATH" ]] || INSTALL_PROBE_PATH=/
INSTALL_TOTAL_KB="$(df -Pk -- "$INSTALL_PROBE_PATH" | awk 'NR==2 {print $2}')"
INSTALL_FREE_KB="$(df -Pk -- "$INSTALL_PROBE_PATH" | awk 'NR==2 {print $4}')"
log "检测到 ${CPU_CORES} 核 CPU、约 ${TOTAL_MEM_MB}MB 内存、安装文件系统可用约 $((INSTALL_FREE_KB / 1024))MB"
[[ "$TOTAL_MEM_MB" -ge 1800 ]] || fail "内存不足 2GB，当前约 ${TOTAL_MEM_MB}MB"
[[ "$INSTALL_TOTAL_KB" -ge 45000000 ]] || log "警告：安装文件系统总容量低于推荐的 50GB"
[[ "$INSTALL_FREE_KB" -ge 8000000 ]] || fail "安装文件系统可用空间不足 8GB，无法安全安装"

export DEBIAN_FRONTEND=noninteractive
log "安装基础软件"
apt-get -o DPkg::Lock::Timeout=120 update -y
apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends \
  ca-certificates curl gnupg tar gzip fuse3 kmod openssh-client

configure_docker_repository() {
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" \
    -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  printf '%s\n' \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
}

if ! command -v docker >/dev/null 2>&1; then
  log "通过 Docker 官方 APT 仓库安装 Docker"
  configure_docker_repository
  apt-get install -y \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
    docker-compose-plugin
fi
systemctl enable --now docker

if ! docker compose version >/dev/null 2>&1; then
  log "Docker Compose 插件不存在，尝试从 Docker 官方 APT 仓库安装"
  configure_docker_repository
  apt-get install -y docker-compose-plugin
fi
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 不可用"

DOCKER_ROOT_DIR="$(docker info --format '{{.DockerRootDir}}')" \
  || fail "无法读取 Docker 数据目录"
[[ "$DOCKER_ROOT_DIR" == /* && "$DOCKER_ROOT_DIR" != *$'\n'* && "$DOCKER_ROOT_DIR" != *$'\r'* ]] \
  || fail "Docker 数据目录不是安全的绝对路径"
DOCKER_PROBE_PATH="$DOCKER_ROOT_DIR"
while [[ ! -e "$DOCKER_PROBE_PATH" ]]; do
  PARENT_PATH="$(dirname -- "$DOCKER_PROBE_PATH")"
  [[ "$PARENT_PATH" != "$DOCKER_PROBE_PATH" ]] || break
  DOCKER_PROBE_PATH="$PARENT_PATH"
done
[[ -e "$DOCKER_PROBE_PATH" ]] || DOCKER_PROBE_PATH=/
INSTALL_FREE_KB="$(df -Pk -- "$INSTALL_PROBE_PATH" | awk 'NR==2 {print $4}')"
[[ "$INSTALL_FREE_KB" -ge 8000000 ]] \
  || fail "安装 Docker 后安装文件系统可用空间不足 8GB，停止部署"
if [[ "$(stat -c %d -- "$INSTALL_PROBE_PATH")" != "$(stat -c %d -- "$DOCKER_PROBE_PATH")" ]]; then
  DOCKER_FREE_KB="$(df -Pk -- "$DOCKER_PROBE_PATH" | awk 'NR==2 {print $4}')"
  DEPLOY_CLOUDDRIVE2_REQUESTED="$(
    awk -F= '$1=="DEPLOY_CLOUDDRIVE2" {print $2; exit}' "$CONFIG_FILE"
  )"
  DEPLOY_CLOUDDRIVE2_REQUESTED="${DEPLOY_CLOUDDRIVE2_REQUESTED%$'\r'}"
  if [[ -f "$INSTALL_DIR/docker-compose.yml" ]]; then
    DOCKER_MIN_FREE_KB=2000000
    [[ -n "$DEPLOY_CLOUDDRIVE2_REQUESTED" && "$DEPLOY_CLOUDDRIVE2_REQUESTED" != true ]] \
      || DOCKER_MIN_FREE_KB=3000000
  else
    DOCKER_MIN_FREE_KB=4000000
    [[ -n "$DEPLOY_CLOUDDRIVE2_REQUESTED" && "$DEPLOY_CLOUDDRIVE2_REQUESTED" != true ]] \
      || DOCKER_MIN_FREE_KB=6000000
  fi
  log "Docker 数据目录位于另一文件系统，可用约 $((DOCKER_FREE_KB / 1024))MB"
  [[ "$DOCKER_FREE_KB" -ge "$DOCKER_MIN_FREE_KB" ]] \
    || fail "Docker 数据文件系统空间不足，无法安全构建和启动容器"
fi

log "准备持久化目录"
mkdir -p \
  "$INSTALL_DIR" \
  "$INSTALL_DIR/data" \
  "$INSTALL_DIR/downloads" \
  "$INSTALL_DIR/logs" \
  "$INSTALL_DIR/config/rclone" \
  "$INSTALL_DIR/clouddrive/config" \
  "$INSTALL_DIR/clouddrive/mounts"
install -d -m 700 /opt/tg115-backups

RELEASE_STAGE="$(mktemp -d /opt/tg115-release-XXXXXXXX)"
CANDIDATE_TAG="tg115-candidate:$(date +%s)-$$"
log "在隔离目录预检新程序和配置"
cp -a "$SOURCE_DIR/." "$RELEASE_STAGE/"
install -m 600 "$CONFIG_FILE" "$RELEASE_STAGE/.env"
# Accept configuration produced by older Windows deployers as well.
sed -i 's/\r$//' "$RELEASE_STAGE/.env"
(cd "$RELEASE_STAGE" && docker compose config --quiet)
docker build --tag "$CANDIDATE_TAG" "$RELEASE_STAGE"
docker run --rm --read-only --tmpfs /tmp --entrypoint python \
  --env-file "$RELEASE_STAGE/.env" "$CANDIDATE_TAG" \
  -m app.deployment_check --validate-only

if [[ -f "$INSTALL_DIR/docker-compose.yml" ]]; then
  BACKUP="$(mktemp /opt/tg115-backups/config-XXXXXXXX.tar.gz)"
  log "备份现有程序配置到 $BACKUP"
  tar -czf "$BACKUP" -C "$INSTALL_DIR" \
    --exclude='./data' \
    --exclude='./downloads' \
    --exclude='./logs' \
    --exclude='./clouddrive' \
    . || fail "配置备份失败，尚未替换程序文件；请先检查磁盘和权限"
  tar -tzf "$BACKUP" >/dev/null || fail "配置备份无法读取，停止升级"
  OLD_IMAGE_ID="$(docker inspect --format '{{.Image}}' tg115-bot 2>/dev/null || true)"
  if [[ -n "$OLD_IMAGE_ID" ]]; then
    OLD_IMAGE_TAG="$(docker inspect --format '{{.Config.Image}}' tg115-bot 2>/dev/null || true)"
    [[ "$OLD_IMAGE_TAG" != "<no value>" ]] || OLD_IMAGE_TAG=""
    ROLLBACK_ARMED=true
    docker stop --time 30 tg115-bot >/dev/null
  fi
  if [[ -f "$INSTALL_DIR/data/tg115.db" ]]; then
    DATABASE_BACKUP_CANDIDATE="$(mktemp /opt/tg115-backups/database-XXXXXXXX.db)"
    rm -f -- "$DATABASE_BACKUP_CANDIDATE"
    DB_TEMP_NAME=".upgrade-backup-$$.db"
    log "创建并校验升级前数据库一致性快照"
    docker run --rm --read-only --tmpfs /tmp --user 10001:10001 \
      --volume "$INSTALL_DIR/data:/data" --entrypoint python "$CANDIDATE_TAG" \
      -m app.backup_database /data/tg115.db "/data/$DB_TEMP_NAME"
    install -m 600 "$INSTALL_DIR/data/$DB_TEMP_NAME" "$DATABASE_BACKUP_CANDIDATE"
    DATABASE_BACKUP="$DATABASE_BACKUP_CANDIDATE"
    rm -f -- "$INSTALL_DIR/data/$DB_TEMP_NAME"
  fi
  ROLLBACK_ARMED=true
fi

log "提交已预检的程序和配置"
remove_program_files
cp -a "$RELEASE_STAGE/." "$INSTALL_DIR/"
chmod 700 \
  "$INSTALL_DIR" \
  "$INSTALL_DIR/data" \
  "$INSTALL_DIR/downloads" \
  "$INSTALL_DIR/logs" \
  "$INSTALL_DIR/config" \
  "$INSTALL_DIR/clouddrive" \
  "$INSTALL_DIR/clouddrive/config" \
  "$INSTALL_DIR/clouddrive/mounts" \
  /opt/tg115-backups
chown -R 10001:10001 \
  "$INSTALL_DIR/data" \
  "$INSTALL_DIR/downloads" \
  "$INSTALL_DIR/logs" \
  "$INSTALL_DIR/config"
chmod +x \
  "$INSTALL_DIR/remote_install.sh" \
  "$INSTALL_DIR/manage.sh" \
  "$INSTALL_DIR/repair_clouddrive_network.sh"

cd "$INSTALL_DIR"
docker compose config --quiet

DEPLOY_CLOUDDRIVE2="$(awk -F= '$1=="DEPLOY_CLOUDDRIVE2" {print $2; exit}' .env)"
[[ -z "$DEPLOY_CLOUDDRIVE2" || "$DEPLOY_CLOUDDRIVE2" == true || "$DEPLOY_CLOUDDRIVE2" == false ]] \
  || fail "DEPLOY_CLOUDDRIVE2 必须为 true 或 false"
if [[ "${DEPLOY_CLOUDDRIVE2:-true}" == "true" ]]; then
  modprobe fuse 2>/dev/null || true
  [[ -c /dev/fuse ]] || fail "VPS 没有提供 /dev/fuse；请让服务商开启 FUSE 后重试"
  mapfile -t CD2_MATCHES < <(
    docker ps --format '{{.Names}}|{{.Image}}|{{.Ports}}' \
      | awk -F'|' '
          tolower($1) ~ /clouddrive2/ ||
          tolower($2) ~ /(^|\/)clouddrive2([:@]|$)/ ||
          tolower($2) ~ /cloudnas\/clouddrive2([:@]|$)/ {
            print $1
          }
        '
  )
  [[ "${#CD2_MATCHES[@]}" -le 1 ]] \
    || fail "发现多个正在运行的 CloudDrive2 容器，请只保留目标容器后重试"
  EXISTING_CD2="${CD2_MATCHES[0]:-}"
  PORT_19798_CONTAINER="$(
    docker ps --format '{{.Names}}|{{.Ports}}' \
      | awk -F'|' '$2 ~ /:19798->/ {print $1; exit}'
  )"
  if [[ -z "$EXISTING_CD2" && -n "$PORT_19798_CONTAINER" ]]; then
    fail "容器 $PORT_19798_CONTAINER 占用 19798 端口，但无法确认它是 CloudDrive2；拒绝自动修改"
  fi
  if [[ -n "$EXISTING_CD2" ]]; then
    log "发现现有 CloudDrive2 容器 $EXISTING_CD2；保留登录和挂载数据"
  else
    log "拉取并启动 CloudDrive2"
    docker compose pull clouddrive2
    docker compose up -d clouddrive2
  fi
fi

log "构建 Telegram → 115 Bot 容器"
docker compose build tg115-bot
bash "$INSTALL_DIR/manage.sh" validate
docker compose up -d --no-deps tg115-bot

log "等待 Bot 基础健康检查"
BOT_HEALTH=""
for _ in $(seq 1 40); do
  BOT_HEALTH="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' tg115-bot 2>/dev/null || true)"
  if [[ "$BOT_HEALTH" == "healthy" ]]; then
    break
  fi
  if [[ "$BOT_HEALTH" == "unhealthy" ]] || [[ "$BOT_HEALTH" == "exited" ]]; then
    break
  fi
  sleep 3
done

if [[ "$BOT_HEALTH" != "healthy" ]]; then
  log "Bot 当前状态：${BOT_HEALTH:-unknown}"
  docker compose ps || true
  docker compose logs --tail=120 tg115-bot || true
  fail "Bot 未通过基础健康检查，请根据上面的日志修正配置"
fi

if [[ "${DEPLOY_CLOUDDRIVE2:-true}" == "true" ]]; then
  log "迁移或修复 CloudDrive2 Docker 网络并执行硬性连通检查"
  export INSTALL_DIR
  bash "$INSTALL_DIR/repair_clouddrive_network.sh" --network-only
fi

log "核对运行中的环境配置、代码指纹和基础健康状态"
bash "$INSTALL_DIR/manage.sh" ready
ROLLBACK_ARMED=false
# shellcheck disable=SC1091
source "$INSTALL_DIR/backup_retention.sh"
if BACKUP_INVENTORY="$(tg115_backup_inventory /opt/tg115-backups)"; then
  BACKUP_COUNT="$(awk -F= '$1=="TOTAL_COUNT" {print $2}' <<< "$BACKUP_INVENTORY")"
  BACKUP_BYTES="$(awk -F= '$1=="TOTAL_BYTES" {print $2}' <<< "$BACKUP_INVENTORY")"
  log "当前保留 ${BACKUP_COUNT:-0} 份升级／配置备份，占用约 $(( ${BACKUP_BYTES:-0} / 1024 / 1024 ))MB"
  if [[ "${BACKUP_BYTES:-0}" =~ ^[0-9]+$ ]] && ((BACKUP_BYTES > 5368709120)); then
    log "警告：历史备份已超过 5GB；请先运行 manage.sh backups 核对，再按需手动 prune-backups"
  fi
else
  log "警告：无法统计历史备份；部署已经通过验收，请稍后运行 manage.sh backups 检查"
fi
log "部署完成"
docker compose ps
printf 'TG115_INSTALL_DIR=%s\n' "$INSTALL_DIR"
printf 'TG115_BOT_HEALTH=%s\n' "$BOT_HEALTH"
printf 'TG115_RESULT=SUCCESS\n'
