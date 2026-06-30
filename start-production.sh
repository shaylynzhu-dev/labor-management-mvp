#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
PROJECT_ROOT="$(pwd)"
VENV="$PROJECT_ROOT/.venv"
LOG_DIR="$PROJECT_ROOT/logs"
PID_FILE="$LOG_DIR/gunicorn.pid"
LOG_FILE="$LOG_DIR/app.log"

mkdir -p "$LOG_DIR"
touch "$LOG_FILE"

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi

. "$VENV/bin/activate"
python -m pip install -r requirements.txt

export LABOUR_OS_ENV=production
if [ -z "${LABOUR_OS_SECRET_KEY:-}" ]; then
  if [ ! -f .labour_os_secret ]; then
    umask 077
    python -c 'import secrets; print(secrets.token_hex(32))' > .labour_os_secret
  fi
  LABOUR_OS_SECRET_KEY="$(tr -d '\r\n' < .labour_os_secret)"
  export LABOUR_OS_SECRET_KEY
fi
export LABOUR_OS_ADMIN_USERNAME="${LABOUR_OS_ADMIN_USERNAME:-admin}"
export LABOUR_OS_ADMIN_PASSWORD="${LABOUR_OS_ADMIN_PASSWORD:-admin123}"

for OLD_PID_FILE in "$PID_FILE" "$PROJECT_ROOT/server.pid"; do
  if [ -f "$OLD_PID_FILE" ]; then
    OLD_PID="$(tr -cd '0-9' < "$OLD_PID_FILE")"
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
      kill "$OLD_PID" 2>/dev/null || true
      for _ in 1 2 3 4 5; do
        kill -0 "$OLD_PID" 2>/dev/null || break
        sleep 1
      done
    fi
    rm -f "$OLD_PID_FILE"
  fi
done

PORT_PID="$(lsof -t -iTCP:5001 -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
if [ -n "$PORT_PID" ]; then
  PORT_CWD="$(lsof -a -p "$PORT_PID" -d cwd -Fn 2>/dev/null |
    sed -n 's/^n//p' | head -n 1)"
  if [ "$PORT_CWD" = "$PROJECT_ROOT" ]; then
    kill "$PORT_PID" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      kill -0 "$PORT_PID" 2>/dev/null || break
      sleep 1
    done
  fi
fi

if lsof -nP -iTCP:5001 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "启动失败：端口 5001 被非本项目进程占用。"
  lsof -nP -iTCP:5001 -sTCP:LISTEN
  exit 1
fi

nohup gunicorn -w 2 -b 0.0.0.0:5001 app:app >> "$LOG_FILE" 2>&1 &
GUNICORN_PID=$!
echo "$GUNICORN_PID" > "$PID_FILE"

READY=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS http://127.0.0.1:5001/health >/dev/null 2>&1; then
    READY=1
    break
  fi
  if ! kill -0 "$GUNICORN_PID" 2>/dev/null; then
    break
  fi
  sleep 1
done

if [ "$READY" -ne 1 ]; then
  echo "启动失败，请检查：$LOG_FILE"
  tail -n 40 "$LOG_FILE" 2>/dev/null || true
  exit 1
fi

echo "Labour OS 已启动：http://127.0.0.1:5001"
echo "Gunicorn PID：$GUNICORN_PID"
echo "日志：$LOG_FILE"
