#!/usr/bin/env bash
set -Eeuo pipefail
export VENUS_LAUNCH_CWD="$PWD"
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
export PYTHONDONTWRITEBYTECODE=1
python_bin="${VENUS_PYTHON:-$PWD/runtime/venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
  echo "Run bash install.sh first." >&2
  exit 1
fi
exec "$python_bin" -B -m demos "$@"
