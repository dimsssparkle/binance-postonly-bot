#!/usr/bin/env bash
# Деплой на боевой Hetzner-сервер: синхронизирует app/ и static/, перезапускает
# сервис, показывает healthz. Заменяет ручные rsync+ssh команды.
#
# Использование: scripts/deploy_hetzner.sh [--no-restart]

set -euo pipefail

HOST="root@65.109.175.194"
SSH_KEY="$HOME/.ssh/id_ed25519_hetzner_bot"
REMOTE_DIR="/opt/bot"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SSH_OPTS=(-i "$SSH_KEY")
RESTART=1
if [[ "${1:-}" == "--no-restart" ]]; then
  RESTART=0
fi

echo "==> Syncing app/ ..."
rsync -az --exclude='.venv' --exclude='.venv311' --exclude='__pycache__' \
  --exclude='.git' --exclude='*.db' --exclude='*.db-shm' --exclude='*.db-wal' \
  -e "ssh ${SSH_OPTS[*]}" \
  "$REPO_ROOT/app/" "$HOST:$REMOTE_DIR/app/"

echo "==> Syncing static/ ..."
rsync -az -e "ssh ${SSH_OPTS[*]}" \
  "$REPO_ROOT/static/" "$HOST:$REMOTE_DIR/static/"

echo "==> Syncing scripts/ ..."
rsync -az -e "ssh ${SSH_OPTS[*]}" \
  "$REPO_ROOT/scripts/" "$HOST:$REMOTE_DIR/scripts/"

if [[ "$RESTART" -eq 1 ]]; then
  echo "==> Restarting binance-bot ..."
  ssh "${SSH_OPTS[@]}" "$HOST" "systemctl restart binance-bot && sleep 2 && systemctl is-active binance-bot"
  echo "==> Healthz check ..."
  ssh "${SSH_OPTS[@]}" "$HOST" "curl -s http://127.0.0.1:8000/healthz && echo"
else
  echo "==> Skipped restart (--no-restart)"
fi

echo "==> Done."
