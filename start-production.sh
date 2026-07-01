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

exec gunicorn app:app \
  --bind "0.0.0.0:${PORT}" \
  --workers "${WEB_CONCURRENCY}" \
  --threads "${GUNICORN_THREADS}" \
  --timeout 120 \
  --access-logfile - \
  --error-logfile - \
  --capture-output
