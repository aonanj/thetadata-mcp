#!/usr/bin/env bash
set -euo pipefail
plugin_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "${plugin_dir}"
exec "${THETADATA_PYTHON:-python3}" -m uvicorn http_app:create_app --factory \
  --app-dir server --host 0.0.0.0 --port "${PORT:-10000}" --workers 1
