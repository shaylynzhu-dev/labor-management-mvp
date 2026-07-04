#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
PROJECT_ROOT="$(pwd)"

export LABOUR_OS_ENV="production"

PORT="${PORT:-10000}"
LABOUR_OS_DATABASE_PATH="${LABOUR_OS_DATABASE_PATH:-${PROJECT_ROOT}/labor.db}"
WEB_CONCURRENCY="${WEB_CONCURRENCY:-1}"
GUNICORN_THREADS="${GUNICORN_THREADS:-4}"
export LABOUR_OS_DATABASE_PATH

echo "启动端口: ${PORT}"
echo "database path: ${LABOUR_OS_DATABASE_PATH}"
echo "production mode: ${LABOUR_OS_ENV}"

mkdir -p logs

python -m app.background.worker_manager >> logs/worker.log 2>&1 &
WORKER_MANAGER_PID=$!

gunicorn app:app \
  --bind "0.0.0.0:${PORT}" \
  --workers "${WEB_CONCURRENCY}" \
  --threads "${GUNICORN_THREADS}" \
  --timeout 120 \
  --access-logfile - \
  --error-logfile - \
  --capture-output &
GUNICORN_PID=$!

cleanup() {
  kill -TERM "${WORKER_MANAGER_PID}" "${GUNICORN_PID}" 2>/dev/null || true
  wait "${WORKER_MANAGER_PID}" "${GUNICORN_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
wait "${GUNICORN_PID}"
