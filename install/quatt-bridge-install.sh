#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Quatt CIC → InfluxDB 3 Bridge  –  Container installer
# Runs INSIDE the LXC container. Called by ct/quatt-bridge.sh.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/antongitnow/quatt-influxdb-bridge/main"

BL='\033[36m'; GN='\033[1;92m'; RD='\033[01;31m'; CL='\033[m'
msg_info()  { echo -e " ${BL}[i]${CL} $*"; }
msg_ok()    { echo -e " ${GN}[✓]${CL} $*"; }
msg_error() { echo -e " ${RD}[✗]${CL} $*"; }

[[ -f /etc/quatt-bridge/config.env ]] || {
  msg_error "/etc/quatt-bridge/config.env not found – run ct/quatt-bridge.sh on the PVE host."
  exit 1
}

# ── System update + Python ─────────────────────────────────────────────────────
msg_info "Updating system packages…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq --no-install-recommends \
  python3 python3-pip python3-venv curl ca-certificates
msg_ok "System ready."

# ── App directory ──────────────────────────────────────────────────────────────
msg_info "Installing bridge application…"
mkdir -p /opt/quatt-bridge

curl -fsSL "${REPO_RAW}/bridge.py"          -o /opt/quatt-bridge/bridge.py
curl -fsSL "${REPO_RAW}/requirements.txt"   -o /opt/quatt-bridge/requirements.txt

python3 -m venv /opt/quatt-bridge/venv
/opt/quatt-bridge/venv/bin/pip install --quiet --upgrade pip
/opt/quatt-bridge/venv/bin/pip install --quiet -r /opt/quatt-bridge/requirements.txt
msg_ok "Bridge application installed."

# ── Service user ───────────────────────────────────────────────────────────────
id bridge &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin bridge
chown -R bridge:bridge /opt/quatt-bridge
chown root:bridge /etc/quatt-bridge/config.env

# ── Systemd service ────────────────────────────────────────────────────────────
msg_info "Installing systemd service…"
curl -fsSL "${REPO_RAW}/bridge.service" -o /etc/systemd/system/quatt-bridge.service

systemctl daemon-reload
systemctl enable quatt-bridge
systemctl start quatt-bridge
sleep 3

STATUS=$(systemctl is-active quatt-bridge 2>/dev/null || true)
if [[ "${STATUS}" == "active" ]]; then
  msg_ok "quatt-bridge service is running."
else
  msg_error "Service failed to start (status: ${STATUS}). Check: journalctl -u quatt-bridge"
  exit 1
fi
