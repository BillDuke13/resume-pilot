#!/usr/bin/env bash
set -euo pipefail

APP_USER="${RESUME_PILOT_USER:-resume-pilot}"
APP_HOME="/home/${APP_USER}"
VNC_CONFIG_DIR="${APP_HOME}/.config/tigervnc"
VNC_PASSWORD_FILE="${RESUME_PILOT_VNC_PASSWORD_FILE:-/root/resume-pilot-vnc-password}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "bootstrap_debian.sh must run as root." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  dbus-x11 \
  fonts-noto-cjk \
  fonts-noto-color-emoji \
  fonts-wqy-zenhei \
  git \
  iproute2 \
  openssl \
  pipx \
  procps \
  psmisc \
  novnc \
  python3-pip \
  python3-venv \
  rsync \
  sudo \
  tigervnc-common \
  tigervnc-standalone-server \
  tigervnc-tools \
  websockify \
  xauth \
  xfce4 \
  xfce4-terminal \
  chromium

if ! id "${APP_USER}" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "${APP_USER}"
fi

install -d -m 700 -o "${APP_USER}" -g "${APP_USER}" "${VNC_CONFIG_DIR}"
install -d -m 700 -o "${APP_USER}" -g "${APP_USER}" \
  "${APP_HOME}/.local/share/resume-pilot" \
  "${APP_HOME}/.local/state/resume-pilot"
chown -R "${APP_USER}:${APP_USER}" "${APP_HOME}/.config" "${APP_HOME}/.local"

if [[ -d "${APP_HOME}/.vnc" && ! -L "${APP_HOME}/.vnc" ]]; then
  legacy_dir="${APP_HOME}/.vnc.legacy.$(date +%Y%m%d%H%M%S)"
  mv "${APP_HOME}/.vnc" "${legacy_dir}"
  chown -R "${APP_USER}:${APP_USER}" "${legacy_dir}"
fi

if [[ ! -f "${VNC_PASSWORD_FILE}" ]]; then
  umask 077
  openssl rand -base64 18 >"${VNC_PASSWORD_FILE}"
fi

vnc_password="$(tr -d '\n' <"${VNC_PASSWORD_FILE}")"
printf '%s\n' "${vnc_password}" | vncpasswd -f >"${VNC_CONFIG_DIR}/passwd"
chown "${APP_USER}:${APP_USER}" "${VNC_CONFIG_DIR}/passwd"
chmod 600 "${VNC_CONFIG_DIR}/passwd"

cat >"${VNC_CONFIG_DIR}/xstartup" <<'SCRIPT'
#!/bin/sh
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
exec startxfce4
SCRIPT
chown "${APP_USER}:${APP_USER}" "${VNC_CONFIG_DIR}/xstartup"
chmod 700 "${VNC_CONFIG_DIR}/xstartup"

if ! runuser -u "${APP_USER}" -- bash -lc 'command -v uv >/dev/null 2>&1'; then
  runuser -u "${APP_USER}" -- python3 -m pipx install --force uv
fi

if [[ -x "${APP_HOME}/.local/bin/uv" ]]; then
  ln -sf "${APP_HOME}/.local/bin/uv" /usr/local/bin/uv
fi

echo "Provisioning complete for ${APP_USER}."
echo "VNC is configured for localhost-only display :1. The password is stored at:"
echo "  ${VNC_PASSWORD_FILE}"
