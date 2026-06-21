#!/usr/bin/env bash
set -euo pipefail

APP_USER="${RESUME_PILOT_USER:-resume-pilot}"
DISPLAY_NUMBER="${RESUME_PILOT_VNC_DISPLAY:-1}"
GEOMETRY="${RESUME_PILOT_VNC_GEOMETRY:-1600x1000}"
DEPTH="${RESUME_PILOT_VNC_DEPTH:-24}"

run_as_user() {
  if [[ "${EUID}" -eq 0 ]]; then
    runuser -u "${APP_USER}" -- bash -lc "$*"
  else
    bash -lc "$*"
  fi
}

if [[ "${EUID}" -ne 0 ]]; then
  APP_USER="$(id -un)"
fi

run_as_user \
  "vncserver -useold -localhost yes -geometry '${GEOMETRY}' -depth '${DEPTH}' :${DISPLAY_NUMBER}"
run_as_user "vncserver -list"
