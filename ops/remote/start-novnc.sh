#!/usr/bin/env bash
set -euo pipefail

APP_USER="${RESUME_PILOT_USER:-resume-pilot}"
APP_HOME="/home/${APP_USER}"
STATE_DIR="${APP_HOME}/.local/state/resume-pilot"
NO_VNC_HOST="${RESUME_PILOT_NOVNC_HOST:-127.0.0.1}"
NO_VNC_PORT="${RESUME_PILOT_NOVNC_PORT:-6080}"
VNC_HOST="${RESUME_PILOT_VNC_HOST:-127.0.0.1}"
VNC_PORT="${RESUME_PILOT_VNC_PORT:-5901}"
WEB_DIR="${RESUME_PILOT_NOVNC_WEB:-/usr/share/novnc}"

run_as_user() {
  if [[ "${EUID}" -eq 0 ]]; then
    runuser -u "${APP_USER}" -- bash -lc "$*"
  else
    bash -lc "$*"
  fi
}

if [[ "${EUID}" -ne 0 ]]; then
  APP_USER="$(id -un)"
  APP_HOME="${HOME}"
  STATE_DIR="${APP_HOME}/.local/state/resume-pilot"
fi

if ! command -v websockify >/dev/null 2>&1; then
  echo "websockify is not installed. Run ops/remote/bootstrap_debian.sh as root." >&2
  exit 1
fi

if [[ ! -d "${WEB_DIR}" ]]; then
  echo "noVNC web directory not found: ${WEB_DIR}" >&2
  exit 1
fi

install -d -m 700 -o "${APP_USER}" -g "${APP_USER}" "${STATE_DIR}"

if ss -ltn | awk '{print $4}' | grep -qE "(^|:)${NO_VNC_PORT}$"; then
  echo "noVNC is already listening on ${NO_VNC_HOST}:${NO_VNC_PORT}."
  exit 0
fi

run_as_user "nohup websockify --web='${WEB_DIR}' '${NO_VNC_HOST}:${NO_VNC_PORT}' \
  '${VNC_HOST}:${VNC_PORT}' >'${STATE_DIR}/novnc.log' 2>&1 & echo \$! >'${STATE_DIR}/novnc.pid'"

for _ in $(seq 1 20); do
  if ss -ltn | awk '{print $4}' | grep -qE "(^|:)${NO_VNC_PORT}$"; then
    echo "noVNC is listening on ${NO_VNC_HOST}:${NO_VNC_PORT}."
    exit 0
  fi
  sleep 0.25
done

echo "noVNC did not start. Log follows:" >&2
run_as_user "tail -100 '${STATE_DIR}/novnc.log' 2>/dev/null || true" >&2
exit 1
