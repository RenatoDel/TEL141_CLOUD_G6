#!/usr/bin/env bash
set -euo pipefail

echo "[1] Sysctl para forwarding/NAT"
sudo mkdir -p /etc/sysctl.d

cat > /tmp/99-pucp-cloud.conf <<'EOC'
net.ipv4.ip_forward=1
net.ipv4.conf.all.rp_filter=0
net.ipv4.conf.default.rp_filter=0
net.bridge.bridge-nf-call-iptables=1
vm.overcommit_memory=1
EOC

sudo mv /tmp/99-pucp-cloud.conf /etc/sysctl.d/99-pucp-cloud.conf
sudo modprobe br_netfilter || true
sudo sysctl --system

echo "[2] Directorios base"
sudo mkdir -p /var/lib/vms/disks /var/lib/vms/seeds /var/lib/vms/cloudinit

echo "[3] Preparando workers"
for host in 10.0.10.1 10.0.10.2 10.0.10.3; do
  echo "  -> $host"
  ssh -i /root/.ssh/id_ecdsa ubuntu@"$host" '
    sudo mkdir -p /var/lib/vms/disks /var/lib/vms/seeds /var/lib/vms/cloudinit
    sudo sysctl -w net.ipv4.ip_forward=1 >/dev/null
  '
done

echo "[4] Estado final del headnode"
sysctl net.ipv4.ip_forward
sysctl vm.overcommit_memory
sudo ovs-vsctl show
