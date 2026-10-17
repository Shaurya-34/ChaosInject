#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
export OPERATOR_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export DATA_MODE=demo
export FAKE_RUNTIME_ENABLED=false
.venv/bin/python -m uvicorn app.demo_target:app --host 127.0.0.1 --port 8090 --no-access-log --log-level critical &
target_pid=$!
.venv/bin/python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log --log-level critical &
api_pid=$!
trap 'kill "$target_pid" "$api_pid" 2>/dev/null || true; unset OPERATOR_TOKEN' EXIT INT TERM
printf 'Open http://127.0.0.1:8000\nTemporary operator token: %s\n' "$OPERATOR_TOKEN"
wait "$api_pid"
