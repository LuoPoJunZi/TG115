#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SOURCE_DIR="${1:-}"
CONFIG_FILE="${2:-}"
INSTALL_DIR="${INSTALL_DIR:-/opt/tg115}"

log() {
  printf '[TG115] %s\n' "$*"
}

fail() {
  trap - ERR
  printf '[TG115][ERROR] %s\n' "$*" >&2
  printf 'TG115_RESULT=FAILED\n' >&2
  exit 1
}

trap 'fail "安装在第 ${LINENO} 行失败。请保留完整日志。"' ERR

[[ "$(id -u)" -eq 0 ]] || fail "remote_install.sh 必须以 root 或 sudo 运行"
[[ -d "$SOURCE_DIR" ]] || fail "找不到部署源目录：$SOURCE_DIR"
[[ -f "$CONFIG_FILE" ]] || fail "找不到配置文件：$CONFIG_FILE"
[[ "$INSTALL_DIR" =~ ^/opt/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$ ]] \
  || fail "安装目录必须是 /opt/ 下的安全绝对路径"
[[ "/$INSTALL_DIR/" != *"/../"* && "/$INSTALL_DIR/" != *"/./"* ]] \
  || fail "安装目录不能包含 . 或 .. 路径段"

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
ROOT_TOTAL_KB="$(df -Pk / | awk 'NR==2 {print $2}')"
ROOT_FREE_KB="$(df -Pk / | awk 'NR==2 {print $4}')"
[[ "$TOTAL_MEM_MB" -ge 1800 ]] || fail "内存不足 2GB，当前约 ${TOTAL_MEM_MB}MB"
[[ "$ROOT_TOTAL_KB" -ge 45000000 ]] || log "警告：系统盘总容量低于推荐的 50GB"
[[ "$ROOT_FREE_KB" -ge 8000000 ]] || fail "系统盘可用空间不足 8GB，无法安全安装"

export DEBIAN_FRONTEND=noninteractive
log "安装基础软件"
apt-get update -y
apt-get install -y --no-install-recommends \
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

if [[ -f "$INSTALL_DIR/docker-compose.yml" ]]; then
  BACKUP="/opt/tg115-backups/config-$(date +%Y%m%d-%H%M%S).tar.gz"
  log "备份现有程序配置到 $BACKUP"
  tar -czf "$BACKUP" -C "$INSTALL_DIR" \
    --exclude='./data' \
    --exclude='./downloads' \
    --exclude='./logs' \
    --exclude='./clouddrive' \
    . || true
fi

log "安装或更新程序文件"
rm -rf -- "$INSTALL_DIR/app"
rm -f -- \
  "$INSTALL_DIR/.dockerignore" \
  "$INSTALL_DIR/Dockerfile" \
  "$INSTALL_DIR/docker-compose.yml" \
  "$INSTALL_DIR/manage.sh" \
  "$INSTALL_DIR/remote_install.sh" \
  "$INSTALL_DIR/repair_clouddrive_network.sh" \
  "$INSTALL_DIR/requirements.txt"
cp -a "$SOURCE_DIR/." "$INSTALL_DIR/"
install -m 600 "$CONFIG_FILE" "$INSTALL_DIR/.env"
# Accept configuration produced by older Windows deployers as well.
sed -i 's/\r$//' "$INSTALL_DIR/.env"
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

# shellcheck disable=SC1091
source "$INSTALL_DIR/.env"
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
docker compose up -d tg115-bot

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

log "部署完成"
docker compose ps
printf 'TG115_INSTALL_DIR=%s\n' "$INSTALL_DIR"
printf 'TG115_BOT_HEALTH=%s\n' "$BOT_HEALTH"
printf 'TG115_RESULT=SUCCESS\n'
