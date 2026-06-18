#!/usr/bin/env bash
# ============================================================================
# install_node_exporter.sh
#
# Instala y arranca node_exporter (binario standalone, sin Docker) en los
# 6 workers del proyecto: server1/2/3 (Linux) y worker1/2/3 (OpenStack).
#
# Por qué standalone y no contenedor: los workers no necesariamente tienen
# Docker instalado (son hosts KVM/OpenStack compute, no hosts de gestión),
# así que un binario systemd es más portable y no introduce esa dependencia.
#
# Idempotente: si node_exporter ya está instalado y corriendo, lo detecta
# y no hace nada (salvo reportarlo).
#
# Uso:
#   ./install_node_exporter.sh
#
# Requiere: que SSH sin contraseña YA esté configurado hacia el gateway
# para los workers Linux (según tu README, sección 2.1). Para los workers
# OpenStack, si piden contraseña, el script te la solicitará una vez por
# worker (no se guarda en ningún lado).
# ============================================================================

set -uo pipefail

GATEWAY_HOST="${GATEWAY_HOST:-10.20.11.189}"
SSH_USER="${SSH_USER:-ubuntu}"
NODE_EXPORTER_VERSION="1.8.2"
NODE_EXPORTER_TARBALL="node_exporter-${NODE_EXPORTER_VERSION}.linux-amd64.tar.gz"
NODE_EXPORTER_URL="https://github.com/prometheus/node_exporter/releases/download/v${NODE_EXPORTER_VERSION}/${NODE_EXPORTER_TARBALL}"

# name:puerto_ssh_gateway
WORKERS=(
  "server1:5811"
  "server2:5812"
  "server3:5813"
  "worker1:5822"
  "worker2:5823"
  "worker3:5824"
)

green()  { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
red()    { printf "\033[31m%s\033[0m\n" "$*"; }

REMOTE_SCRIPT=$(cat << 'REMOTE_EOF'
set -e

VERSION="__VERSION__"
TARBALL="node_exporter-${VERSION}.linux-amd64.tar.gz"
URL="https://github.com/prometheus/node_exporter/releases/download/v${VERSION}/${TARBALL}"

if systemctl is-active --quiet node_exporter 2>/dev/null; then
  echo "ALREADY_RUNNING"
  exit 0
fi

cd /tmp
if [ ! -f "/tmp/${TARBALL}" ]; then
  wget -q "$URL" -O "/tmp/${TARBALL}" || { echo "DOWNLOAD_FAILED"; exit 1; }
fi

tar xzf "/tmp/${TARBALL}" -C /tmp
sudo mv "/tmp/node_exporter-${VERSION}.linux-amd64/node_exporter" /usr/local/bin/node_exporter
sudo chmod +x /usr/local/bin/node_exporter

sudo tee /etc/systemd/system/node_exporter.service > /dev/null << 'UNIT_EOF'
[Unit]
Description=Prometheus Node Exporter
After=network.target

[Service]
User=node_exporter
Group=node_exporter
Type=simple
ExecStart=/usr/local/bin/node_exporter --web.listen-address=0.0.0.0:9100

[Install]
WantedBy=multi-user.target
UNIT_EOF

sudo useradd -rs /bin/false node_exporter 2>/dev/null || true
sudo systemctl daemon-reload
sudo systemctl enable node_exporter
sudo systemctl restart node_exporter
sleep 1
systemctl is-active --quiet node_exporter && echo "INSTALLED_OK" || echo "START_FAILED"
REMOTE_EOF
)
REMOTE_SCRIPT="${REMOTE_SCRIPT/__VERSION__/$NODE_EXPORTER_VERSION}"

FAILED_WORKERS=()

for entry in "${WORKERS[@]}"; do
  name="${entry%%:*}"
  port="${entry##*:}"

  yellow "=== ${name} (puerto ${port}) ==="
  result=$(ssh -p "$port" -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new \
    "${SSH_USER}@${GATEWAY_HOST}" "bash -s" <<< "$REMOTE_SCRIPT" 2>&1)
  status_line=$(echo "$result" | tail -1)

  case "$status_line" in
    ALREADY_RUNNING)
      green "  node_exporter ya estaba corriendo en ${name}"
      ;;
    INSTALLED_OK)
      green "  node_exporter instalado y corriendo en ${name}"
      ;;
    DOWNLOAD_FAILED)
      red "  FALLÓ la descarga del binario en ${name} (¿sin salida a Internet?)"
      FAILED_WORKERS+=("$name")
      ;;
    START_FAILED)
      red "  El servicio no arrancó en ${name}. Salida completa:"
      echo "$result"
      FAILED_WORKERS+=("$name")
      ;;
    *)
      red "  Resultado inesperado en ${name}:"
      echo "$result"
      FAILED_WORKERS+=("$name")
      ;;
  esac
done

echo
if [ ${#FAILED_WORKERS[@]} -eq 0 ]; then
  green "═══════════════════════════════════════════════════"
  green "  node_exporter instalado correctamente en los 6 workers"
  green "═══════════════════════════════════════════════════"
else
  red "═══════════════════════════════════════════════════"
  red "  Fallaron: ${FAILED_WORKERS[*]}"
  red "  Revisa la salida de arriba para cada uno"
  red "═══════════════════════════════════════════════════"
  exit 1
fi
