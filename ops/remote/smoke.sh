#!/usr/bin/env bash
set -euo pipefail

APP_USER="${RESUME_PILOT_USER:-resume-pilot}"
APP_DIR="${RESUME_PILOT_APP_DIR:-/home/${APP_USER}/resume-pilot}"
SOURCE_URL="${RESUME_PILOT_SMOKE_SOURCE_URL:-https://www.zhipin.com/web/geek/job}"

run_as_user() {
  if [[ "${EUID}" -eq 0 ]]; then
    runuser -u "${APP_USER}" -- bash -lc "$*"
  else
    bash -lc "$*"
  fi
}

run_as_user "cd '${APP_DIR}' && uv sync --all-groups"
run_as_user "cd '${APP_DIR}' && uv run pytest"
run_as_user "cd '${APP_DIR}' && uv run ruff check ."
run_as_user "cd '${APP_DIR}' && uv run resume-pilot doctor"

"$(dirname "$0")/start-vnc.sh"
"$(dirname "$0")/start-browser.sh"
"$(dirname "$0")/start-novnc.sh"

run_as_user "cd '${APP_DIR}' && DISPLAY=:1 uv run resume-pilot doctor"
run_as_user "cd '${APP_DIR}' && uv run resume-pilot run --dry-run \
  --decision-fixture apply \
  --source-url '${SOURCE_URL}' \
  --html-file ops/fixtures/boss_jobs.html \
  --limit 1"
run_as_user "cd '${APP_DIR}' && uv run resume-pilot inbox watch --dry-run \
  --html-file ops/fixtures/boss_inbox.html"
run_as_user "python3 - <<'PY'
from urllib.request import urlopen

with urlopen('http://127.0.0.1:6080/vnc.html', timeout=5) as response:
    body = response.read(256).decode('utf-8', errors='ignore')
    assert response.status == 200
    assert 'noVNC' in body or '<!DOCTYPE html>' in body
print('noVNC HTTP smoke passed')
PY"

if [[ "${RESUME_PILOT_PROBE_LLM:-0}" == "1" ]]; then
  run_as_user "cd '${APP_DIR}' && uv run resume-pilot doctor --probe-llm"
fi
