#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

INSTALL_DIR="${INSTALL_DIR:-/opt/tg115}"
[[ "$INSTALL_DIR" =~ ^/opt/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$ ]] || exit 2
[[ "/$INSTALL_DIR/" != *"/../"* && "/$INSTALL_DIR/" != *"/./"* ]] || exit 2
[[ "$(realpath -e -- "$INSTALL_DIR")" == "$INSTALL_DIR" ]] || exit 2
cd "$INSTALL_DIR"
# shellcheck disable=SC1091
source ./backup_retention.sh

check_config() {
  local fingerprint
  fingerprint="$(sha256sum app/*.py | LC_ALL=C sort -k2 | sha256sum | cut -d' ' -f1)"
  docker compose config --format json \
    | docker compose exec -T tg115-bot python -m app.deployment_check \
        --expected-code "$fingerprint"
}

validate_config() {
  docker compose config --quiet || return 1
  docker compose run --rm --no-deps --entrypoint python tg115-bot \
    -m app.deployment_check --validate-only
}

wait_healthy() {
  local health
  for _ in $(seq 1 40); do
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' tg115-bot 2>/dev/null || true)"
    if [[ "$health" == healthy ]]; then
      check_config
      return
    fi
    [[ "$health" != unhealthy && "$health" != exited ]] || return 1
    sleep 3
  done
  return 1
}

apply_config() {
  local candidate="${2:-}" backup staged restarted=false
  [[ -f "$candidate" && "$candidate" == /* ]] || { echo "请提供新配置文件的绝对路径" >&2; return 2; }
  install -d -m 700 /opt/tg115-backups
  backup="$(mktemp /opt/tg115-backups/env-XXXXXXXX.env)"
  install -m 600 .env "$backup"
  restore_config() {
    install -m 600 "$backup" .env
    if [[ "$restarted" == true ]]; then
      if ! docker compose up -d --no-deps tg115-bot || ! wait_healthy; then
        echo '旧配置已恢复，但容器恢复未通过核验，请人工检查。' >&2
      fi
    fi
    if [[ -n "${staged:-}" && -f "$staged" ]]; then
      rm -f -- "$staged"
    fi
  }
  trap 'trap - ERR INT TERM HUP; restore_config; exit 1' ERR INT TERM HUP
  staged="$(mktemp "$INSTALL_DIR/.env-staged-XXXXXXXX")"
  install -m 600 "$candidate" "$staged"
  sed -i 's/\r$//' "$staged"
  mv -f -- "$staged" .env
  if validate_config; then
    restarted=true
    if docker compose up -d --no-deps tg115-bot && wait_healthy; then
      trap - ERR INT TERM HUP
      printf 'TG115_APPLY_CONFIG=OK\n配置备份：%s\n' "$backup"
      return
    fi
  fi
  restore_config
  trap - ERR INT TERM HUP
  printf 'TG115_APPLY_CONFIG=FAILED\n已恢复旧配置；备份：%s\n' "$backup" >&2
  return 1
}

case "${1:-status}" in
  status)
    docker compose ps
    ;;
  logs)
    docker compose logs --since=30m --tail=200 -f tg115-bot
    ;;
  restart)
    docker compose restart tg115-bot
    ;;
  stop)
    docker compose stop tg115-bot
    ;;
  start)
    docker compose up -d tg115-bot
    wait_healthy
    ;;
  update)
    docker compose build tg115-bot
    validate_config
    docker compose up -d --no-deps tg115-bot
    wait_healthy
    ;;
  apply-config)
    apply_config "$@"
    ;;
  backups)
    tg115_backup_inventory /opt/tg115-backups
    ;;
  prune-backups)
    tg115_prune_backups /opt/tg115-backups "${2:-5}"
    ;;
  check|doctor)
    check_config
    docker compose exec -T tg115-bot python -m app.healthcheck
    ;;
  validate)
    validate_config
    ;;
  ready)
    wait_healthy
    ;;
  verify)
    docker compose exec -T tg115-bot python -m app.verify_destination
    ;;
  *)
    echo "用法：$0 {status|logs|restart|stop|start|update|verify|check|doctor|validate|backups|prune-backups [1-50]|apply-config /absolute/config.env}" >&2
    exit 2
    ;;
esac
