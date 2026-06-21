#!/usr/bin/env bash
set -euo pipefail

APP_USER="${RESUME_PILOT_USER:-resume-pilot}"
APP_DIR="${RESUME_PILOT_APP_DIR:-/home/${APP_USER}/resume-pilot}"
DISPLAY_VALUE="${DISPLAY:-:1}"
GEOMETRY="${RESUME_PILOT_VNC_GEOMETRY:-1600x1000}"

run_as_user() {
  if [[ "${EUID}" -eq 0 ]]; then
    runuser -u "${APP_USER}" -- bash -lc "$*"
  else
    bash -lc "$*"
  fi
}

run_command="cd '${APP_DIR}' && export DISPLAY='${DISPLAY_VALUE}' && \
  export RESUME_PILOT_CHROME_BIN=\"\${RESUME_PILOT_CHROME_BIN:-chromium}\" && \
  uv run resume-pilot browser start"

run_as_user "${run_command}"

if command -v xdotool >/dev/null 2>&1 && [[ "${GEOMETRY}" =~ ^[0-9]+x[0-9]+$ ]]; then
  width="${GEOMETRY%x*}"
  height="${GEOMETRY#*x}"
  run_as_user "export DISPLAY='${DISPLAY_VALUE}'; \
    for _ in \$(seq 1 20); do \
      wid=\$(xdotool search --onlyvisible --class chromium 2>/dev/null | tail -n 1 || true); \
      if [[ -n \"\$wid\" ]]; then \
        xdotool windowactivate --sync \"\$wid\"; \
        xdotool windowmove \"\$wid\" 0 0; \
        xdotool windowsize \"\$wid\" '${width}' '${height}'; \
        exit 0; \
      fi; \
      sleep 0.25; \
    done"
fi
