#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/tg115}"
cd "$INSTALL_DIR"

case "${1:-status}" in
  status)
    docker compose ps
    ;;
  logs)
    docker compose logs --tail=200 -f tg115-bot
    ;;
  restart)
    docker compose restart tg115-bot
    ;;
  stop)
    docker compose stop tg115-bot
    ;;
  start)
    docker compose up -d tg115-bot
    ;;
  update)
    docker compose build tg115-bot
    docker compose up -d tg115-bot
    ;;
  verify)
    docker compose exec -T tg115-bot python -m app.verify_destination
    ;;
  *)
    echo "用法：$0 {status|logs|restart|stop|start|update|verify}" >&2
    exit 2
    ;;
esac
