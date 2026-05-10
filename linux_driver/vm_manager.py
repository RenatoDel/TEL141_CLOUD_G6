from __future__ import annotations
"""
vm_manager.py
-------------
Gestión de VMs usando qemu-system-x86_64 directamente.
Basado en create_vm.sh y delete_vm.sh del laboratorio 4.

Rutas en los servidores (igual que los scripts):
    Imagen base : /var/lib/vms/images/cirros-base.img
    Discos delta: /var/lib/vms/disks/{vm_name}.qcow2
    PID files   : /var/run/qemu-{vm_name}.pid
    Monitor sock: /var/run/qemu-{vm_name}.monitor
    TAP iface   : tap-{ultimos 11 chars del vm_name}
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

from ssh_client import SSHClient

logger = logging.getLogger(__name__)

IMAGE_DIR      = "/var/lib/vms/images"
DISK_DIR       = "/var/lib/vms/disks"
BASE_IMAGE     = "cirros-base.img"
BASE_IMAGE_URL = "https://download.cirros-cloud.net/0.6.2/cirros-0.6.2-x86_64-disk.img"


def _tap_name(vm_name: str) -> str:
    """
    Genera el nombre de la interfaz TAP para una VM.
    Linux tiene límite de 15 caracteres para nombres de interfaces.
    tap- = 4 chars, quedan 11 para el nombre de la VM.
    """
    return f"tap-{vm_name[-11:]}"


@dataclass
class VMConfig:
    """Configuración de una VM a crear."""
    name:       str
    ovs_bridge: str
    vlan_id:    int
    vnc_port:   int
    ram_mb:     int = 256
    vcpus:      int = 1


class VMManager:
    """
    Gestiona VMs QEMU en un servidor de cómputo.
    Replica la lógica de create_vm.sh y delete_vm.sh.
    """

    def __init__(self, ssh: SSHClient):
        self.ssh = ssh

    # ------------------------------------------------------------------
    # Crear VM
    # ------------------------------------------------------------------

    def create_vm(self, config: VMConfig):
        """
        Crea y arranca una VM. Replica create_vm.sh exactamente:
        1. Asegura que existen los directorios
        2. Descarga imagen base si no existe
        3. Crea disco delta QCOW2 con backing file
        4. Crea interfaz TAP
        5. Conecta TAP al OVS con tag VLAN
        6. Lanza QEMU en background con daemonize
        """
        logger.info(f"[VMManager] Creando VM {config.name} en {self.ssh.host}")

        base_path = f"{IMAGE_DIR}/{BASE_IMAGE}"
        disk_path = f"{DISK_DIR}/{config.name}.qcow2"
        tap       = _tap_name(config.name)
        pid_file  = f"/var/run/qemu-{config.name}.pid"
        monitor   = f"/var/run/qemu-{config.name}.monitor"
        vnc_disp  = config.vnc_port - 5900

        logger.debug(f"[VMManager] TAP name: {tap} ({len(tap)} chars)")

        # 1. Crear directorios
        self.ssh.sudo(f"mkdir -p {IMAGE_DIR} {DISK_DIR}")

        # 2. Descargar imagen base si no existe
        if not self.ssh.file_exists(base_path):
            logger.info(f"[VMManager] Descargando imagen base cirros en {self.ssh.host}...")
            self.ssh.sudo(
                f"wget -q -O {base_path} {BASE_IMAGE_URL}",
                timeout=300,
            )
            logger.info(f"[VMManager] Imagen base descargada: {base_path}")
        else:
            logger.debug(f"[VMManager] Imagen base ya existe: {base_path}")

        # 3. Crear disco delta
        if self.ssh.file_exists(disk_path):
            raise FileExistsError(
                f"El disco {disk_path} ya existe en {self.ssh.host}. "
                f"Borra la VM primero con delete_vm()."
            )
        self.ssh.sudo(
            f"qemu-img create -f qcow2 -b {base_path} -F qcow2 {disk_path}"
        )
        logger.debug(f"[VMManager] Disco delta creado: {disk_path}")

        # 4. Crear interfaz TAP si no existe
        out, _ = self.ssh.execute(
            f"ip link show {tap} 2>/dev/null && echo exists || echo missing",
            raise_on_error=False,
        )
        if "missing" in out:
            self.ssh.sudo(f"ip tuntap add dev {tap} mode tap")
            self.ssh.sudo(f"ip link set {tap} up")
            logger.debug(f"[VMManager] TAP {tap} creada")
        else:
            logger.debug(f"[VMManager] TAP {tap} ya existe")

        # 5. Conectar TAP al OVS con tag VLAN
        ports_out, _ = self.ssh.sudo(
            f"ovs-vsctl list-ports {config.ovs_bridge}",
            raise_on_error=False,
        )
        if tap not in ports_out.split():
            self.ssh.sudo(
                f"ovs-vsctl add-port {config.ovs_bridge} {tap} tag={config.vlan_id}"
            )
            logger.debug(f"[VMManager] TAP {tap} conectada a {config.ovs_bridge} VLAN {config.vlan_id}")
        else:
            logger.debug(f"[VMManager] TAP {tap} ya está en {config.ovs_bridge}")

        # 6. Lanzar QEMU
        qemu_cmd = (
            f"qemu-system-x86_64 "
            f"-name {config.name} "
            f"-m {config.ram_mb} "
            f"-smp {config.vcpus} "
            f"-drive file={disk_path},format=qcow2,if=virtio "
            f"-netdev tap,id=net0,ifname={tap},script=no,downscript=no "
            f"-device virtio-net-pci,netdev=net0 "
            f"-vnc :{vnc_disp} "
            f"-daemonize "
            f"-pidfile {pid_file} "
            f"-monitor unix:{monitor},server,nowait"
        )
        self.ssh.sudo(qemu_cmd)
        logger.info(
            f"[VMManager] VM {config.name} corriendo. "
            f"VNC en {self.ssh.host}:{config.vnc_port}"
        )

    # ------------------------------------------------------------------
    # Borrar VM
    # ------------------------------------------------------------------

    def delete_vm(self, vm_name: str, ovs_bridge: str):
        """
        Elimina una VM y todos sus recursos.
        Replica delete_vm.sh exactamente:
        1. Mata el proceso QEMU via pidfile
        2. Desconecta TAP del OVS
        3. Elimina interfaz TAP
        4. Elimina disco delta
        5. Si no quedan más deltas, elimina imagen base
        """
        logger.info(f"[VMManager] Eliminando VM {vm_name} en {self.ssh.host}")

        disk_path = f"{DISK_DIR}/{vm_name}.qcow2"
        tap       = _tap_name(vm_name)
        pid_file  = f"/var/run/qemu-{vm_name}.pid"
        monitor   = f"/var/run/qemu-{vm_name}.monitor"

        # 1. Matar proceso QEMU
        if self.ssh.file_exists(pid_file):
            pid_out, _ = self.ssh.execute(f"cat {pid_file}", raise_on_error=False)
            pid = pid_out.strip()
            if pid:
                self.ssh.execute(
                    f"kill -0 {pid} 2>/dev/null && sudo kill {pid} || true",
                    raise_on_error=False,
                )
                time.sleep(3)
            self.ssh.sudo(f"rm -f {pid_file}")
            logger.debug(f"[VMManager] Proceso QEMU {pid} terminado")
        else:
            logger.debug(f"[VMManager] No hay pidfile para {vm_name}")

        # Limpiar monitor socket
        self.ssh.sudo(f"rm -f {monitor}", raise_on_error=False)

        # 2. Desconectar TAP del OVS
        ports_out, _ = self.ssh.sudo(
            f"ovs-vsctl list-ports {ovs_bridge}",
            raise_on_error=False,
        )
        if tap in ports_out.split():
            self.ssh.sudo(
                f"ovs-vsctl del-port {ovs_bridge} {tap}",
                raise_on_error=False,
            )
            logger.debug(f"[VMManager] TAP {tap} removida de {ovs_bridge}")

        # 3. Eliminar interfaz TAP
        out, _ = self.ssh.execute(
            f"ip link show {tap} 2>/dev/null && echo exists || echo missing",
            raise_on_error=False,
        )
        if "exists" in out:
            self.ssh.sudo(
                f"ip link set {tap} down 2>/dev/null || true",
                raise_on_error=False,
            )
            time.sleep(2)
            self.ssh.sudo(
                f"ip tuntap del dev {tap} mode tap",
                raise_on_error=False,
            )
            logger.debug(f"[VMManager] TAP {tap} eliminada")

        # 4. Eliminar disco delta
        if self.ssh.file_exists(disk_path):
            self.ssh.sudo(f"rm -f {disk_path}")
            logger.debug(f"[VMManager] Disco {disk_path} eliminado")

        # 5. Verificar si quedan más discos delta — si no, eliminar base
        base_path = f"{IMAGE_DIR}/{BASE_IMAGE}"
        delta_count_out, _ = self.ssh.execute(
            f"find {DISK_DIR} -name '*.qcow2' | "
            f"xargs -I{{}} sudo qemu-img info {{}} 2>/dev/null | "
            f"grep -c 'backing file' || echo 0",
            raise_on_error=False,
        )
        try:
            delta_count = int(delta_count_out.strip())
        except ValueError:
            delta_count = 0

        if delta_count == 0 and self.ssh.file_exists(base_path):
            self.ssh.sudo(f"rm -f {base_path}")
            logger.info(f"[VMManager] Sin más deltas — imagen base eliminada")
        else:
            logger.debug(f"[VMManager] Imagen base conservada ({delta_count} delta(s))")

        logger.info(f"[VMManager] VM {vm_name} eliminada correctamente")

    # ------------------------------------------------------------------
    # Estado
    # ------------------------------------------------------------------

    def get_vm_status(self, vm_name: str) -> str:
        """Retorna 'running', 'stopped', o 'unknown'."""
        pid_file = f"/var/run/qemu-{vm_name}.pid"

        if not self.ssh.file_exists(pid_file):
            return "stopped"

        pid_out, _ = self.ssh.execute(f"cat {pid_file}", raise_on_error=False)
        pid = pid_out.strip()

        if not pid:
            return "stopped"

        out, _ = self.ssh.execute(
            f"kill -0 {pid} 2>/dev/null && echo running || echo stopped",
            raise_on_error=False,
        )
        return out.strip()

    def get_vnc_info(self, vm_name: str, vnc_port: int) -> dict:
        """
        Retorna información para conectarse a la consola VNC.
        Túnel SSH desde tu máquina:
            ssh ubuntu@10.20.12.70 -p 5801 -L 5901:localhost:5901
        """
        status = self.get_vm_status(vm_name)
        return {
            "vm_name":    vm_name,
            "status":     status,
            "server":     self.ssh.host,
            "vnc_port":   vnc_port,
            "vnc_tunnel": (
                f"ssh ubuntu@10.20.12.70 -p {self._get_gateway_port()} "
                f"-L {vnc_port}:localhost:{vnc_port}"
            ),
        }

    def list_vms(self) -> list[dict]:
        """Lista todas las VMs corriendo en el servidor."""
        out, _ = self.ssh.execute(
            "sudo ps aux | grep qemu-system | grep -v grep",
            raise_on_error=False,
        )
        vms = []
        for line in out.splitlines():
            if "-name" in line:
                parts = line.split()
                try:
                    name_idx = parts.index("-name")
                    vm_name  = parts[name_idx + 1]
                    vms.append({
                        "name":   vm_name,
                        "status": "running",
                        "server": self.ssh.host,
                    })
                except (ValueError, IndexError):
                    pass
        return vms

    def _get_gateway_port(self) -> int:
        from ssh_client import SERVERS_INTERNAL, SERVERS_VIA_GATEWAY
        for name, ip in SERVERS_INTERNAL.items():
            if ip == self.ssh.host:
                return SERVERS_VIA_GATEWAY.get(name, 22)
        return 22
