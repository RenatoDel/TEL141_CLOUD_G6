from __future__ import annotations

import base64
import ipaddress
import logging
import shlex
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .ssh_client import SSHClient

logger = logging.getLogger(__name__)

IMAGE_DIR = "/var/lib/vms/images"
DISK_DIR = "/var/lib/vms/disks"
SEED_DIR = "/var/lib/vms/seeds"
RUNTIME_DIR = "/var/lib/vms/runtime"


def _tap_name(vm_name: str) -> str:
    return f"tap-{vm_name[-11:]}"


@dataclass
class VMInterface:
    tap_name: str
    vlan_id: int
    ip_cidr: str | None = None
    mac_address: str | None = None
    gateway4: str | None = None
    nameservers: list[str] = field(default_factory=list)
    set_name: str | None = None
    static_routes: list[dict] = field(default_factory=list)


@dataclass
class VMConfig:
    name: str
    ovs_bridge: str
    vnc_port: int
    base_image: str = "cirros-base.img"
    ram_mb: int = 256
    vcpus: int = 1
    disk_gb: int = 10
    vlan_id: int | None = None
    interfaces: list[VMInterface] = field(default_factory=list)
    loopback_cidr: str | None = None
    enable_ip_forward: bool = False
    # slice_id: prefijo que hace únicos los paths de disco/seed/runtime
    # por slice, permitiendo múltiples slices con VMs de mismo nombre.
    # Default "" para compatibilidad con código legado que no lo pasa.
    slice_id: str = ""


def _vm_file_prefix(slice_id: str, vm_name: str) -> str:
    """
    Prefijo único para todos los archivos asociados a una VM específica
    dentro de un slice concreto.

    Con slice_id:  "mi-slice-abc--vm1"  → discos/seeds/pids únicos por slice.
    Sin slice_id:  "vm1"               → comportamiento idéntico al original
                                         (backwards compat con código legado).
    """
    if slice_id:
        # Limpiar caracteres problemáticos en nombres de archivo
        safe_slice = slice_id.replace("/", "-").replace("\\", "-")[:40]
        return f"{safe_slice}--{vm_name}"
    return vm_name


class VMManager:
    def __init__(self, ssh: SSHClient):
        self.ssh = ssh

    def _run_sudo_bash(self, script: str):
        cmd = f"bash -lc {shlex.quote(script)}"
        self.ssh.sudo(cmd)

    def _write_remote_text(self, path: str, content: str):
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        script = f"""
python3 - <<'PY'
import base64
from pathlib import Path
p = Path({path!r})
p.parent.mkdir(parents=True, exist_ok=True)
p.write_bytes(base64.b64decode({b64!r}))
PY
"""
        self._run_sudo_bash(script)

    def _effective_interfaces(self, config: VMConfig) -> list[VMInterface]:
        if config.interfaces:
            return config.interfaces

        if config.vlan_id is None:
            raise ValueError("Debes indicar vlan_id o interfaces")

        return [VMInterface(tap_name=_tap_name(config.name), vlan_id=config.vlan_id)]

    def _is_cirros(self, config: VMConfig) -> bool:
        return "cirros" in config.base_image.lower()

    def _build_user_data(
        self,
        vm_name: str,
        is_cirros: bool,
        loopback_cidr: str | None = None,
        enable_ip_forward: bool = False,
    ) -> str:
        cmds: list[str] = []

        if loopback_cidr:
            cmds.append(f"ip addr add {loopback_cidr} dev lo 2>/dev/null || true")

        if enable_ip_forward:
            cmds.extend(
                [
                    "sysctl -w net.ipv4.ip_forward=1 || true",
                    "sysctl -w net.ipv4.conf.all.rp_filter=0 || true",
                    "sysctl -w net.ipv4.conf.default.rp_filter=0 || true",
                    "iptables -P FORWARD ACCEPT 2>/dev/null || true",
                    "iptables -P INPUT ACCEPT 2>/dev/null || true",
                    "iptables -P OUTPUT ACCEPT 2>/dev/null || true",
                ]
            )

        cmds.append("systemctl restart systemd-networkd 2>/dev/null || true")
        runcmd = "\n".join(f"  - [ sh, -lc, {cmd!r} ]" for cmd in cmds)

        if is_cirros:
            return f"""#cloud-config
hostname: {vm_name}
manage_etc_hosts: true

runcmd:
{runcmd}
"""

        return f"""#cloud-config
hostname: {vm_name}
manage_etc_hosts: true

users:
  - default

ssh_pwauth: true
disable_root: false

chpasswd:
  expire: false
  list: |
    ubuntu:ubuntu

package_update: false
package_upgrade: false

runcmd:
{runcmd}
"""

    def _build_cirros_network_interfaces(self, interfaces: list[VMInterface]) -> str:
        blocks: list[str] = []

        for idx, iface in enumerate(interfaces):
            guest_name = iface.set_name or f"eth{idx}"

            if iface.ip_cidr:
                ipi = ipaddress.ip_interface(iface.ip_cidr)
                netmask = ipi.network.netmask
                lines = [
                    f"auto {guest_name}",
                    f"iface {guest_name} inet static",
                    f"  address {ipi.ip}",
                    f"  netmask {netmask}",
                ]
                if iface.gateway4:
                    lines.append(f"  gateway {iface.gateway4}")
                if iface.nameservers:
                    lines.append(f"  dns-nameservers {' '.join(iface.nameservers)}")
            else:
                lines = [
                    f"auto {guest_name}",
                    f"iface {guest_name} inet dhcp",
                ]

            for route in iface.static_routes:
                to = route.get("to")
                via = route.get("via")
                if to and via:
                    lines.append(f"  up ip route replace {to} via {via} dev {guest_name} || true")

            blocks.append("\n".join(lines))

        return "\n\n".join(blocks)

    def _build_meta_data(self, vm_name: str, interfaces: list[VMInterface], is_cirros: bool) -> str:
        lines = [
            f"instance-id: {vm_name}",
            f"local-hostname: {vm_name}",
        ]

        if is_cirros:
            eni = self._build_cirros_network_interfaces(interfaces)
            lines.append("network-interfaces: |")
            for line in eni.splitlines():
                lines.append(f"  {line}")

        return "\n".join(lines) + "\n"

    def _build_network_config(self, interfaces: list[VMInterface]) -> str:
        lines = [
            "version: 2",
            "ethernets:",
        ]

        for idx, iface in enumerate(interfaces):
            guest_name = iface.set_name or f"ens{idx + 3}"
            lines.append(f"  {guest_name}:")



            if iface.ip_cidr:
                lines.extend(
                    [
                        "    dhcp4: false",
                        "    addresses:",
                        f"      - {iface.ip_cidr}",
                    ]
                )
            else:
                lines.append("    dhcp4: true")

            route_entries: list[dict] = []

            if iface.gateway4:
                route_entries.append({"to": "default", "via": iface.gateway4})

            for route in iface.static_routes:
                to = route.get("to")
                via = route.get("via")
                if to and via:
                    item = {"to": to, "via": via}
                    if item not in route_entries:
                        route_entries.append(item)

            if route_entries:
                lines.append("    routes:")
                for route in route_entries:
                    lines.append(f"      - to: {route['to']}")
                    lines.append(f"        via: {route['via']}")

            if iface.nameservers:
                lines.extend(
                    [
                        "    nameservers:",
                        "      addresses:",
                    ]
                )
                for ns in iface.nameservers:
                    lines.append(f"        - {ns}")

        return "\n".join(lines) + "\n"

    def _create_seed_image(self, config: VMConfig, interfaces: list[VMInterface]) -> str:
        is_cirros = self._is_cirros(config)

        _fp = _vm_file_prefix(config.slice_id, config.name)
        work_dir = str(PurePosixPath(SEED_DIR) / _fp)
        user_data_path = str(PurePosixPath(work_dir) / "user-data")
        meta_data_path = str(PurePosixPath(work_dir) / "meta-data")
        network_config_path = str(PurePosixPath(work_dir) / "network-config")
        seed_img_path = str(PurePosixPath(SEED_DIR) / f"{_fp}-seed.img")

        self.ssh.sudo(f"mkdir -p {SEED_DIR}")
        self.ssh.sudo(f"rm -rf {work_dir} {seed_img_path}", raise_on_error=False)
        self.ssh.sudo(f"mkdir -p {work_dir}")

        self._write_remote_text(
            user_data_path,
            self._build_user_data(
                config.name,
                is_cirros,
                config.loopback_cidr,
                config.enable_ip_forward,
            ),
        )
        self._write_remote_text(meta_data_path, self._build_meta_data(config.name, interfaces, is_cirros))

        if is_cirros:
            self.ssh.sudo(
                f"cloud-localds {seed_img_path} {user_data_path} {meta_data_path}"
            )
        else:
            self._write_remote_text(network_config_path, self._build_network_config(interfaces))
            self.ssh.sudo(
                f"cloud-localds --network-config={network_config_path} "
                f"{seed_img_path} {user_data_path} {meta_data_path}"
            )

        self.ssh.sudo(f"chmod 644 {seed_img_path}", raise_on_error=False)
        return seed_img_path

    def _write_launcher_script(self, config: VMConfig, qemu_cmd: str):
        launch_path = str(PurePosixPath(RUNTIME_DIR) / f"{config.name}.sh")
        pid_file = f"/var/run/qemu-{config.name}.pid"
        monitor = f"/var/run/qemu-{config.name}.monitor"
        serial_path = f"/var/run/qemu-{config.name}.serial"

        content = f"""#!/usr/bin/env bash
set -e
rm -f {pid_file} {monitor} {serial_path}
{qemu_cmd}
"""
        self.ssh.sudo(f"mkdir -p {RUNTIME_DIR}")
        self._write_remote_text(launch_path, content)
        self.ssh.sudo(f"chmod 755 {launch_path}")
        return launch_path

    def _pid_file(self, vm_name: str) -> str:
        return f"/var/run/qemu-{vm_name}.pid"

    def _monitor_path(self, vm_name: str) -> str:
        return f"/var/run/qemu-{vm_name}.monitor"

    def _serial_path(self, vm_name: str) -> str:
        return f"/var/run/qemu-{vm_name}.serial"

    def _launch_path(self, vm_name: str) -> str:
        return str(PurePosixPath(RUNTIME_DIR) / f"{vm_name}.sh")

    def _read_pid(self, vm_name: str) -> int | None:
        pid_file = self._pid_file(vm_name)
        if not self.ssh.file_exists(pid_file):
            return None
        out, _ = self.ssh.sudo(f"cat {pid_file}", raise_on_error=False)
        text = out.strip()
        if not text.isdigit():
            return None
        return int(text)

    def create_vm(self, config: VMConfig):
        base_path = str(PurePosixPath(IMAGE_DIR) / config.base_image)
        _fp = _vm_file_prefix(config.slice_id, config.name)
        disk_path = str(PurePosixPath(DISK_DIR) / f"{_fp}.qcow2")
        seed_img_path = str(PurePosixPath(SEED_DIR) / f"{_fp}-seed.img")
        pid_file = self._pid_file(config.name)
        monitor = self._monitor_path(config.name)
        serial_path = self._serial_path(config.name)
        vnc_disp = config.vnc_port - 5900

        interfaces = self._effective_interfaces(config)

        self.ssh.sudo(f"mkdir -p {IMAGE_DIR} {DISK_DIR} {SEED_DIR} {RUNTIME_DIR}")

        if not self.ssh.file_exists(base_path):
            raise FileNotFoundError(f"La imagen base no existe en el worker: {base_path}")

        if self.ssh.file_exists(disk_path):
            raise FileExistsError(f"El overlay ya existe: {disk_path}")

        self.ssh.sudo(f"qemu-img create -f qcow2 -b {base_path} -F qcow2 {disk_path}")
        self.ssh.sudo(f"qemu-img resize {disk_path} {config.disk_gb}G")

        ports_out, _ = self.ssh.sudo(
            f"ovs-vsctl list-ports {config.ovs_bridge}",
            raise_on_error=False,
        )
        existing_ports = set(ports_out.split())

        for iface in interfaces:
            out, _ = self.ssh.execute(
                f"ip link show {iface.tap_name} 2>/dev/null && echo exists || echo missing",
                raise_on_error=False,
            )
            if "missing" in out:
                self.ssh.sudo(f"ip tuntap add dev {iface.tap_name} mode tap")
                self.ssh.sudo(f"ip link set {iface.tap_name} up")

            if iface.tap_name not in existing_ports:
                if iface.vlan_id == 0:
                    # sin tag VLAN — acceso directo al OFS para provider network
                    self.ssh.sudo(
                        f"ovs-vsctl add-port {config.ovs_bridge} {iface.tap_name}"
                    )
                else:
                    self.ssh.sudo(
                        f"ovs-vsctl add-port {config.ovs_bridge} {iface.tap_name} tag={iface.vlan_id}"
                    )

        self._create_seed_image(config, interfaces)

        qemu_parts = [
            "qemu-system-x86_64",
            f"-name {config.name}",
            f"-m {config.ram_mb}",
            f"-smp {config.vcpus}",
            f"-drive file={disk_path},format=qcow2,if=virtio",
            f"-drive file={seed_img_path},format=raw,if=virtio,media=disk",
        ]

        for idx, iface in enumerate(interfaces):
            qemu_parts.append(
                f"-netdev tap,id=net{idx},ifname={iface.tap_name},script=no,downscript=no"
            )
            dev = f"-device virtio-net-pci,netdev=net{idx}"
            if iface.mac_address:
                dev += f",mac={iface.mac_address}"
            qemu_parts.append(dev)

        qemu_parts.extend(
            [
                f"-vnc :{vnc_disp}",
                f"-monitor unix:{monitor},server,nowait",
                f"-serial unix:{serial_path},server,nowait",
                "-daemonize",
                f"-pidfile {pid_file}",
            ]
        )

        qemu_cmd = " ".join(qemu_parts)
        self._write_launcher_script(config, qemu_cmd)
        self.ssh.sudo(qemu_cmd)

        logger.info(
            "VM %s lanzada en %s VNC:%s base=%s interfaces=%s",
            config.name,
            self.ssh.host,
            config.vnc_port,
            config.base_image,
            [iface.tap_name for iface in interfaces],
        )

    def start_vm(self, vm_name: str) -> str:
        status = self.get_vm_status(vm_name)
        if status in {"running", "paused"}:
            return status

        launch_path = self._launch_path(vm_name)
        if not self.ssh.file_exists(launch_path):
            raise FileNotFoundError(f"No existe launcher para {vm_name}: {launch_path}")

        self.ssh.sudo(
            f"rm -f {self._pid_file(vm_name)} {self._monitor_path(vm_name)} {self._serial_path(vm_name)}",
            raise_on_error=False,
        )
        self.ssh.sudo(f"bash {launch_path}")
        time.sleep(2)
        return self.get_vm_status(vm_name)

    def stop_vm(self, vm_name: str, force: bool = True, timeout: int = 12) -> str:
        pid = self._read_pid(vm_name)
        if not pid:
            return "stopped"

        self.ssh.execute(f"sudo kill {pid} 2>/dev/null || true", raise_on_error=False)

        end = time.time() + timeout
        while time.time() < end:
            if self.get_vm_status(vm_name) == "stopped":
                return "stopped"
            time.sleep(1)

        if force:
            self.ssh.execute(f"sudo kill -9 {pid} 2>/dev/null || true", raise_on_error=False)
            time.sleep(1)

        return self.get_vm_status(vm_name)

    def reboot_vm(self, vm_name: str) -> str:
        current = self.get_vm_status(vm_name)
        if current in {"running", "paused"}:
            self.stop_vm(vm_name, force=True)
        return self.start_vm(vm_name)

    def pause_vm(self, vm_name: str) -> str:
        status = self.get_vm_status(vm_name)
        if status == "paused":
            return status
        if status != "running":
            return status

        pid = self._read_pid(vm_name)
        if not pid:
            return "stopped"

        self.ssh.execute(f"sudo kill -STOP {pid} 2>/dev/null || true", raise_on_error=False)
        time.sleep(1)
        return self.get_vm_status(vm_name)

    def resume_vm(self, vm_name: str) -> str:
        status = self.get_vm_status(vm_name)
        if status == "running":
            return status
        if status != "paused":
            return status

        pid = self._read_pid(vm_name)
        if not pid:
            return "stopped"

        self.ssh.execute(f"sudo kill -CONT {pid} 2>/dev/null || true", raise_on_error=False)
        time.sleep(1)
        return self.get_vm_status(vm_name)

    def delete_vm(self, vm_name: str, ovs_bridge: str, interface_names: list[str] | None = None, slice_id: str = ""):
        _fp = _vm_file_prefix(slice_id, vm_name)
        disk_path = str(PurePosixPath(DISK_DIR) / f"{_fp}.qcow2")
        seed_img_path = str(PurePosixPath(SEED_DIR) / f"{_fp}-seed.img")
        seed_dir = str(PurePosixPath(SEED_DIR) / _fp)
        pid_file = self._pid_file(vm_name)
        monitor = self._monitor_path(vm_name)
        serial_path = self._serial_path(vm_name)
        launch_path = self._launch_path(vm_name)

        if self.ssh.file_exists(pid_file):
            pid_out, _ = self.ssh.sudo(f"cat {pid_file}", raise_on_error=False)
            pid = pid_out.strip()
            if pid:
                self.ssh.execute(
                    f"sudo kill -0 {pid} 2>/dev/null && sudo kill {pid} || true",
                    raise_on_error=False,
                )
                time.sleep(2)
            self.ssh.sudo(f"rm -f {pid_file}", raise_on_error=False)

        self.ssh.sudo(f"rm -f {monitor} {serial_path} {launch_path}", raise_on_error=False)

        taps = interface_names[:] if interface_names else [_tap_name(vm_name)]

        ports_out, _ = self.ssh.sudo(
            f"ovs-vsctl list-ports {ovs_bridge}",
            raise_on_error=False,
        )
        existing_ports = set(ports_out.split())

        for tap in taps:
            if tap in existing_ports:
                self.ssh.sudo(
                    f"ovs-vsctl del-port {ovs_bridge} {tap}",
                    raise_on_error=False,
                )
            self.ssh.sudo(f"ip link set {tap} down 2>/dev/null || true", raise_on_error=False)
            self.ssh.sudo(f"ip tuntap del dev {tap} mode tap", raise_on_error=False)

        self.ssh.sudo(f"rm -f {disk_path}", raise_on_error=False)
        self.ssh.sudo(f"rm -f {seed_img_path}", raise_on_error=False)
        self.ssh.sudo(f"rm -rf {seed_dir}", raise_on_error=False)

    def get_vm_status(self, vm_name: str) -> str:
        pid = self._read_pid(vm_name)
        if not pid:
            return "stopped"

        out, _ = self.ssh.execute(
            f"ps -o stat= -p {pid} 2>/dev/null || true",
            raise_on_error=False,
        )
        stat = out.strip()

        if not stat:
            return "stopped"

        if "T" in stat:
            return "paused"

        return "running"
