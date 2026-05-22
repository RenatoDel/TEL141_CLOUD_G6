#!/usr/bin/env bash
set -euo pipefail

WORKERS=(10.0.10.1 10.0.10.2 10.0.10.3)

for host in "${WORKERS[@]}"; do
  echo "========== LIMPIANDO WORKER $host =========="
  ssh -i /root/.ssh/id_ecdsa ubuntu@"$host" 'bash -s' <<'EOSSH'
set -euo pipefail

echo "[1] Matando QEMU del laboratorio"
sudo pkill -9 -f "qemu-system-x86_64" 2>/dev/null || true
sleep 2

echo "[2] Eliminando puertos TAP/TP de OVS"
for port in $(sudo ovs-vsctl list-ports br-int 2>/dev/null | egrep '^(tap-|tp)' || true); do
  sudo ovs-vsctl --if-exists del-port br-int "$port" || true
done

echo "[3] Eliminando interfaces TAP/TP locales"
for dev in $(ip -o link show | awk -F': ' '{print $2}' | cut -d@ -f1 | egrep '^(tap-|tp)' || true); do
  sudo ip link delete "$dev" 2>/dev/null || true
done

echo "[4] Limpiando runtime"
sudo rm -f /var/run/qemu-*.pid /var/run/qemu-*.monitor || true

echo "[5] Limpiando discos y seeds"
sudo rm -f /var/lib/vms/disks/*.qcow2 2>/dev/null || true
sudo rm -rf /var/lib/vms/seeds/* 2>/dev/null || true
sudo rm -rf /var/lib/vms/cloudinit/* 2>/dev/null || true

echo "[6] Estado final"
ps aux | grep qemu-system | grep -v grep || true
sudo ovs-vsctl show || true
sudo ls -la /var/lib/vms/disks 2>/dev/null || true
sudo ls -la /var/lib/vms/seeds 2>/dev/null || true
EOSSH
done

echo "========== LIMPIANDO HEADNODE =========="
sudo pkill -9 -f "qemu-system-x86_64" 2>/dev/null || true
sleep 2

echo "[H1] Eliminando puertos OVS internos del headnode"
for port in $(sudo ovs-vsctl list-ports br-int 2>/dev/null | egrep '^(tap-|tp|vlan|veth-h-|wan|nat)' || true); do
  sudo ovs-vsctl --if-exists del-port br-int "$port" || true
done

echo "[H2] Eliminando interfaces locales"
for dev in $(ip -o link show | awk -F': ' '{print $2}' | cut -d@ -f1 | egrep '^(tap-|tp|veth-h-|vlan|wan|nat)' || true); do
  sudo ip link delete "$dev" 2>/dev/null || true
done

echo "[H3] Eliminando namespaces DHCP/NAT viejos"
for ns in $(sudo ip netns list | awk '{print $1}' | egrep '^(dhcp-ns-|nat-ns-)' || true); do
  sudo ip netns del "$ns" 2>/dev/null || true
done

echo "[H4] Limpiando logs, pidfiles, seeds y estado"
sudo rm -f /var/run/dnsmasq-dhcp-ns-*.pid /var/log/dnsmasq-vlan*.log 2>/dev/null || true
sudo rm -f /var/run/qemu-*.pid /var/run/qemu-*.monitor 2>/dev/null || true
sudo rm -rf /var/lib/vms/seeds/* /var/lib/vms/cloudinit/* 2>/dev/null || true
sudo rm -f /var/lib/vms/disks/*.qcow2 2>/dev/null || true
printf '[]\n' | sudo tee /root/pucp-private-cloud/slice_manager/state/slices.json >/dev/null

echo "[H5] Intentando retirar reglas NAT típicas de pruebas viejas"
for subnet in 10.200.0.0/16 172.30.0.0/16 172.31.0.0/16; do
  sudo iptables -t nat -D POSTROUTING -s "$subnet" -o ens3 -j MASQUERADE 2>/dev/null || true
done

echo "[H6] Estado final"
sudo ovs-vsctl show || true
sudo ip netns list || true
cat /root/pucp-private-cloud/slice_manager/state/slices.json
