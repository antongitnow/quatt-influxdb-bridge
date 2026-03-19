#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Quatt CIC → InfluxDB Bridge  –  Proxmox VE LXC Helper  (supports v2 & v3)
# Usage: bash -c "$(curl -fsSL https://raw.githubusercontent.com/antongitnow/quatt-influxdb-bridge/main/ct/quatt-bridge.sh)"
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/antongitnow/quatt-influxdb-bridge/main"
APP="Quatt CIC Bridge"
APP_ID="quatt-bridge"
UBUNTU_VERSION="22.04"
CT_HOSTNAME="quatt-bridge"
CT_CORES=1
CT_RAM=128
CT_DISK=1    # GB
CT_BRIDGE="vmbr0"

# ── Colours ───────────────────────────────────────────────────────────────────
BL='\033[36m'; GN='\033[1;92m'; RD='\033[01;31m'; YW='\033[33m'; CL='\033[m'
CM="${GN}✓${CL}"; CROSS="${RD}✗${CL}"
msg_info()    { echo -e " ${BL}[i]${CL} $*"; }
msg_ok()      { echo -e " ${CM} $*"; }
msg_error()   { echo -e " ${CROSS} $*"; }
header_info() {
  clear
  cat <<'EOF'

  ██████╗ ██╗   ██╗ █████╗ ████████╗████████╗
 ██╔═══██╗██║   ██║██╔══██╗╚══██╔══╝╚══██╔══╝
 ██║   ██║██║   ██║███████║   ██║      ██║
 ██║▄▄ ██║██║   ██║██╔══██║   ██║      ██║
 ╚██████╔╝╚██████╔╝██║  ██║   ██║      ██║
  ╚══▀▀═╝  ╚═════╝ ╚═╝  ╚═╝   ╚═╝      ╚═╝

 CIC → InfluxDB Bridge (v2 / v3)  |  Proxmox VE Helper
EOF
}

# ── Guards ────────────────────────────────────────────────────────────────────
[[ -f /etc/pve/local/pve-ssl.key ]] || {
  msg_error "This script must run on a Proxmox VE node."
  exit 1
}

command -v pct    >/dev/null || { msg_error "pct not found."; exit 1; }
command -v pveam  >/dev/null || { msg_error "pveam not found."; exit 1; }
command -v pvesh  >/dev/null || { msg_error "pvesh not found."; exit 1; }

header_info
echo ""

# ── Bridge app configuration ──────────────────────────────────────────────────
echo -e " ${YW}Bridge configuration${CL}"
echo "─────────────────────────────────────────────────────"

read -rp "  Quatt CIC IP address                    : " CIC_IP
[[ -n "${CIC_IP}" ]] || { msg_error "CIC IP is required."; exit 1; }

read -rp "  InfluxDB IP address                     : " INFLUXDB_IP
[[ -n "${INFLUXDB_IP}" ]] || { msg_error "InfluxDB IP is required."; exit 1; }

read -rp "  InfluxDB port                    [8086]: " INFLUXDB_PORT
INFLUXDB_PORT="${INFLUXDB_PORT:-8086}"

while true; do
  read -rp "  InfluxDB version                    [2/3]: " INFLUXDB_VERSION
  INFLUXDB_VERSION="${INFLUXDB_VERSION:-3}"
  [[ "${INFLUXDB_VERSION}" == "2" || "${INFLUXDB_VERSION}" == "3" ]] && break
  msg_error "Please enter 2 or 3."
done

# v2 needs an org name; v3 does not use orgs
INFLUXDB_ORG=""
if [[ "${INFLUXDB_VERSION}" == "2" ]]; then
  read -rp "  InfluxDB organisation name              : " INFLUXDB_ORG
  [[ -n "${INFLUXDB_ORG}" ]] || { msg_error "Organisation name is required for InfluxDB v2."; exit 1; }
fi

# v2 calls it a "bucket", v3 calls it a "database"
if [[ "${INFLUXDB_VERSION}" == "2" ]]; then
  read -rp "  InfluxDB bucket                 [quatt]: " INFLUXDB_DB
else
  read -rp "  InfluxDB database               [quatt]: " INFLUXDB_DB
fi
INFLUXDB_DB="${INFLUXDB_DB:-quatt}"

read -rsp "  InfluxDB API token (hidden)             : " INFLUXDB_TOKEN
echo ""
[[ -n "${INFLUXDB_TOKEN}" ]] || { msg_error "InfluxDB API token is required."; exit 1; }

read -rp "  Poll interval seconds            [10]: " POLL_INTERVAL
POLL_INTERVAL="${POLL_INTERVAL:-10}"
[[ "${POLL_INTERVAL}" =~ ^[0-9]+$ ]] || { msg_error "Poll interval must be a positive integer."; exit 1; }

echo ""
echo -e " ${YW}Container configuration${CL}"
echo "─────────────────────────────────────────────────────"

NEXT_ID=$(pvesh get /cluster/nextid 2>/dev/null || pvesh get /nodes/localhost/nextid 2>/dev/null || echo "200")
read -rp "  Container ID                   [${NEXT_ID}]: " CTID
CTID="${CTID:-${NEXT_ID}}"

read -rp "  Hostname               [${CT_HOSTNAME}]: " HOSTNAME_INPUT
CT_HOSTNAME="${HOSTNAME_INPUT:-${CT_HOSTNAME}}"

# Find available storage
DEFAULT_STORAGE="local-lvm"
if ! pvesm status -content rootdir 2>/dev/null | grep -q "local-lvm"; then
  DEFAULT_STORAGE=$(pvesm status -content rootdir 2>/dev/null | awk 'NR==2{print $1}' || echo "local")
fi
read -rp "  Storage                [${DEFAULT_STORAGE}]: " STORAGE_INPUT
CT_STORAGE="${STORAGE_INPUT:-${DEFAULT_STORAGE}}"

read -rp "  Network bridge              [${CT_BRIDGE}]: " BRIDGE_INPUT
CT_BRIDGE="${BRIDGE_INPUT:-${CT_BRIDGE}}"

echo ""
echo -e " ${YW}Summary${CL}"
echo "─────────────────────────────────────────────────────"
echo "  CT ID          : ${CTID}"
echo "  Hostname       : ${CT_HOSTNAME}"
echo "  Storage        : ${CT_STORAGE}"
echo "  Bridge         : ${CT_BRIDGE}"
echo "  RAM / Cores    : ${CT_RAM}MB / ${CT_CORES}"
echo "  Disk           : ${CT_DISK}GB"
echo "  CIC IP         : ${CIC_IP}"
if [[ "${INFLUXDB_VERSION}" == "2" ]]; then
  echo "  InfluxDB v2    : ${INFLUXDB_IP}:${INFLUXDB_PORT}  org=${INFLUXDB_ORG}  bucket=${INFLUXDB_DB}"
else
  echo "  InfluxDB v3    : ${INFLUXDB_IP}:${INFLUXDB_PORT}  database=${INFLUXDB_DB}"
fi
echo "  Token          : $(echo "${INFLUXDB_TOKEN}" | head -c 8)…"
echo "  Poll interval  : ${POLL_INTERVAL}s"
echo ""
read -rp "  Proceed? [y/N]: " CONFIRM
[[ "${CONFIRM,,}" == "y" ]] || { msg_info "Aborted."; exit 0; }
echo ""

# ── Download Ubuntu template ───────────────────────────────────────────────────
msg_info "Checking Ubuntu ${UBUNTU_VERSION} template…"

TEMPLATE_STORAGE="local"
TEMPLATE=$(pveam list "${TEMPLATE_STORAGE}" 2>/dev/null \
  | grep "ubuntu-${UBUNTU_VERSION}" | awk '{print $1}' | sort -V | tail -1)

if [[ -z "${TEMPLATE}" ]]; then
  msg_info "Downloading Ubuntu ${UBUNTU_VERSION} template…"
  AVAIL=$(pveam available --section system 2>/dev/null \
    | grep "ubuntu-${UBUNTU_VERSION}" | awk '{print $2}' | sort -V | tail -1)
  [[ -n "${AVAIL}" ]] || { msg_error "Could not find Ubuntu ${UBUNTU_VERSION} in pveam."; exit 1; }
  pveam download "${TEMPLATE_STORAGE}" "${AVAIL}" >/dev/null
  TEMPLATE="${TEMPLATE_STORAGE}:vztmpl/${AVAIL}"
  msg_ok "Template downloaded: ${AVAIL}"
else
  msg_ok "Template found: $(basename "${TEMPLATE}")"
fi

# ── Create container ───────────────────────────────────────────────────────────
msg_info "Creating LXC container ${CTID}…"

pct create "${CTID}" "${TEMPLATE}" \
  --hostname "${CT_HOSTNAME}" \
  --cores    "${CT_CORES}" \
  --memory   "${CT_RAM}" \
  --rootfs   "${CT_STORAGE}:${CT_DISK}" \
  --net0     "name=eth0,bridge=${CT_BRIDGE},ip=dhcp,ip6=dhcp" \
  --unprivileged 1 \
  --features "nesting=1" \
  --onboot   1 \
  --start    0

msg_ok "Container ${CTID} created."

# ── Write config into container description (for reference) ───────────────────
if [[ "${INFLUXDB_VERSION}" == "2" ]]; then
  pct set "${CTID}" --description \
    "Quatt CIC → InfluxDB 2 bridge. CIC: ${CIC_IP}  InfluxDB: ${INFLUXDB_IP}:${INFLUXDB_PORT}  org: ${INFLUXDB_ORG}  bucket: ${INFLUXDB_DB}"
else
  pct set "${CTID}" --description \
    "Quatt CIC → InfluxDB 3 bridge. CIC: ${CIC_IP}  InfluxDB: ${INFLUXDB_IP}:${INFLUXDB_PORT}  database: ${INFLUXDB_DB}"
fi

# ── Start container ────────────────────────────────────────────────────────────
msg_info "Starting container…"
pct start "${CTID}"

msg_info "Waiting for network…"
sleep 8

# Wait for apt to be available
for i in $(seq 1 20); do
  pct exec "${CTID}" -- bash -c "command -v apt-get" &>/dev/null && break
  sleep 2
done

msg_info "Installing curl…"
pct exec "${CTID}" -- bash -c "apt-get update -qq && apt-get install -y -qq curl"
msg_ok "curl installed."

# ── Write bridge config inside container ──────────────────────────────────────
msg_info "Writing bridge configuration…"
pct exec "${CTID}" -- bash -c "mkdir -p /etc/quatt-bridge"
# Write each value with printf to avoid shell expansion of special chars in token
{
  printf 'CIC_IP=%s\n'           "${CIC_IP}"
  printf 'INFLUXDB_IP=%s\n'      "${INFLUXDB_IP}"
  printf 'INFLUXDB_PORT=%s\n'    "${INFLUXDB_PORT}"
  printf 'INFLUXDB_VERSION=%s\n' "${INFLUXDB_VERSION}"
  printf 'INFLUXDB_ORG=%s\n'     "${INFLUXDB_ORG}"
  printf 'INFLUXDB_DB=%s\n'      "${INFLUXDB_DB}"
  printf 'INFLUXDB_TOKEN=%s\n'   "${INFLUXDB_TOKEN}"
  printf 'POLL_INTERVAL=%s\n'    "${POLL_INTERVAL}"
} | pct exec "${CTID}" -- bash -c "cat > /etc/quatt-bridge/config.env"
pct exec "${CTID}" -- chmod 600 /etc/quatt-bridge/config.env
msg_ok "Configuration written."

# ── Run install script inside container ───────────────────────────────────────
msg_info "Running installer inside container (this takes ~60s)…"
pct exec "${CTID}" -- bash -c \
  "curl -fsSL ${REPO_RAW}/install/quatt-bridge-install.sh | bash" || {
  msg_error "Installer failed. Check logs: pct exec ${CTID} -- journalctl -u quatt-bridge"
  exit 1
}
msg_ok "Installer finished."

# ── Report ────────────────────────────────────────────────────────────────────
CT_IP=$(pct exec "${CTID}" -- bash -c "hostname -I | awk '{print \$1}'" 2>/dev/null || echo "<check: pct exec ${CTID} -- hostname -I>")

echo ""
echo -e " ${GN}─────────────────────────────────────────────────────${CL}"
echo -e " ${GN} ${APP} installed successfully!${CL}"
echo -e " ${GN}─────────────────────────────────────────────────────${CL}"
echo ""
echo "   Container ID : ${CTID}"
echo "   Container IP : ${CT_IP}"
echo ""
echo "   Useful commands:"
echo "   pct exec ${CTID} -- journalctl -u quatt-bridge -f"
echo "   pct exec ${CTID} -- systemctl status quatt-bridge"
echo "   pct exec ${CTID} -- systemctl restart quatt-bridge"
echo ""
