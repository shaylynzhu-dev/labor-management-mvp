#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi

if ! .venv/bin/python -c "import flask, pandas, openpyxl" >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements.txt
fi

PYTHONPYCACHEPREFIX=/tmp/labour-os-pycache .venv/bin/python -m unittest -v
