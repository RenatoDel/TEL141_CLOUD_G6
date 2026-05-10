from __future__ import annotations
from __future__ import annotations
"""
driver.py
---------
Orquestador principal del Linux Driver.
Coordina ssh_client, vm_manager y network_manager
para crear y destruir slices completos.

Arquitectura de servidores:
    server1, server2 → cómputo (corren las VMs)
    server3          → headnode (networking, DHCP, iptables)
    server4          → cliente (ejecuta este código)

Esta es la función de entrada que llamará el RQ Worker.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from ssh_client     import SSHClient, get_client, get_client_internal
from vm_manager     import VMManager, VMConfig
from network_manager import NetworkManager
from topologies      import (
    build_linear_slice, get_linear_summary,
    build_ring_slice,   get_ring_summary,
    SUPPORTED,
)

logger = logging.getLogger(__name__)

# Mapeo de nombre de servidor a IP interna
SERVER_IPS = {
    "server1": "10.0.10.1",
    "server2": "10.0.10.2",
    "server3": "10.0.10.3",  # headnode
}

OVS_BRIDGE = "br-int"


@dataclass
class SliceRequest:
    """Solicitud de creación de un slice."""
    slice_id:     str
    topology:     str          # "linear" o "ring"
    vlan_id:      int
    cidr:         str          # ej: "192.168.100.0/24"
    vm_count:     int
    servers:      list[str]    # a qué servidor va cada VM
    vnc_start:    int = 5901
    has_internet: bool = False
    has_dhcp:     bool = False
    dhcp_start:   str  = ""
    dhcp_end:     str  = ""


@dataclass
class VMResult:
    """Resultado del despliegue de una VM."""
    vm_id:    str
    name:     str
    server:   str
    vnc_port: int
    status:   str
    error:    Optional[str] = None


@dataclass
class SliceResult:
    """Resultado del despliegue de un slice."""
    slice_id: str
    topology: str
    success:  bool
    vms:      list[VMResult] = field(default_factory=list)
    error:    Optional[str]  = None


class LinuxDriver:
    """
    Driver principal para despliegue de slices sobre Linux KVM.

    Recibe la configuración SSH y orquesta la creación/borrado
    de slices en los servidores de la topología VNRT.

    Modo de uso — desde server4 con SSH passwordless:
        driver = LinuxDriver(
            ssh_mode = "internal",
            key_path = "/home/ubuntu/.ssh/id_ecdsa",
        )
        result = driver.create_slice(request)

    Modo de uso — desde tu máquina via gateway:
        driver = LinuxDriver(
            ssh_mode = "gateway",
            password = "tu_password",
        )
        result = driver.create_slice(request)
    """

    def __init__(
        self,
        ssh_mode: str = "internal",   # "internal" o "gateway"
        password: Optional[str] = None,
        key_path: str = "/home/ubuntu/.ssh/id_ecdsa",
    ):
        self.ssh_mode = ssh_mode
        self.password = password
        self.key_path = key_path

    def _get_client(self, server: str) -> SSHClient:
        """Crea un cliente SSH según el modo configurado."""
        if self.ssh_mode == "internal":
            return get_client_internal(server, self.key_path)
        else:
            return get_client(server, self.password)

    # ------------------------------------------------------------------
    # Crear slice
    # ------------------------------------------------------------------

    def create_slice(self, request: SliceRequest) -> SliceResult:
        """
        Crea un slice completo.

        Flujo:
            1. Validar topología
            2. Crear red VLAN en el headnode (server3)
            3. Por cada VM: crearla en su servidor de cómputo
            4. Si has_internet: habilitar salida a internet
            5. Si falla algo: rollback completo

        Retorna SliceResult con el estado de cada VM.
        """
        logger.info(
            f"[LinuxDriver] Creando slice {request.slice_id} "
            f"topología={request.topology} vms={request.vm_count} "
            f"VLAN={request.vlan_id}"
        )

        # Validar topología
        if request.topology not in SUPPORTED:
            return SliceResult(
                slice_id = request.slice_id,
                topology = request.topology,
                success  = False,
                error    = f"Topología '{request.topology}' no soportada. "
                           f"Soportadas: {SUPPORTED}",
            )

        # Construir objeto de topología
        if request.topology == "linear":
            slice_obj = build_linear_slice(
                slice_id     = request.slice_id,
                vlan_id      = request.vlan_id,
                cidr         = request.cidr,
                vm_count     = request.vm_count,
                servers      = request.servers,
                vnc_start    = request.vnc_start,
                has_internet = request.has_internet,
                has_dhcp     = request.has_dhcp,
                dhcp_start   = request.dhcp_start,
                dhcp_end     = request.dhcp_end,
            )
            summary = get_linear_summary(slice_obj)
        else:
            slice_obj = build_ring_slice(
                slice_id     = request.slice_id,
                vlan_id      = request.vlan_id,
                cidr         = request.cidr,
                vm_count     = request.vm_count,
                servers      = request.servers,
                vnc_start    = request.vnc_start,
                has_internet = request.has_internet,
                has_dhcp     = request.has_dhcp,
                dhcp_start   = request.dhcp_start,
                dhcp_end     = request.dhcp_end,
            )
            summary = get_ring_summary(slice_obj)

        logger.info(f"[LinuxDriver] Topología: {summary}")

        created_vms = []
        vm_results  = []

        try:
            # Paso 1: crear red VLAN en headnode
            logger.info(f"[LinuxDriver] Paso 1: configurando red en headnode")
            with self._get_client("server3") as headnode:
                net_mgr = NetworkManager(headnode)
                net_mgr.create_vlan_network(
                    vlan_id    = request.vlan_id,
                    cidr       = request.cidr,
                    dhcp       = request.has_dhcp,
                    dhcp_start = request.dhcp_start or None,
                    dhcp_end   = request.dhcp_end   or None,
                )
                if request.has_internet:
                    net_mgr.enable_internet(request.vlan_id, request.cidr)

            # Paso 2: crear VMs en sus servidores
            logger.info(f"[LinuxDriver] Paso 2: creando VMs")
            for vm in slice_obj.vms:
                result = self._create_single_vm(vm, request.vlan_id)
                vm_results.append(result)

                if result.error:
                    raise RuntimeError(
                        f"Fallo al crear VM {vm.vm_id}: {result.error}"
                    )
                created_vms.append(vm)

            logger.info(
                f"[LinuxDriver] Slice {request.slice_id} creado exitosamente. "
                f"{len(created_vms)} VMs activas."
            )

            return SliceResult(
                slice_id = request.slice_id,
                topology = request.topology,
                success  = True,
                vms      = vm_results,
            )

        except Exception as e:
            logger.error(
                f"[LinuxDriver] Error creando slice {request.slice_id}: {e}. "
                f"Ejecutando rollback..."
            )
            self._rollback(request, created_vms)
            return SliceResult(
                slice_id = request.slice_id,
                topology = request.topology,
                success  = False,
                error    = str(e),
                vms      = vm_results,
            )

    def _create_single_vm(self, vm, vlan_id: int) -> VMResult:
        """Crea una sola VM en su servidor."""
        try:
            with self._get_client(vm.server) as client:
                mgr = VMManager(client)
                config = VMConfig(
                    name       = vm.name,
                    ovs_bridge = OVS_BRIDGE,
                    vlan_id    = vlan_id,
                    vnc_port   = vm.vnc_port,
                )
                mgr.create_vm(config)
                status = mgr.get_vm_status(vm.name)

            return VMResult(
                vm_id    = vm.vm_id,
                name     = vm.name,
                server   = vm.server,
                vnc_port = vm.vnc_port,
                status   = status,
            )

        except Exception as e:
            logger.error(f"[LinuxDriver] Error creando {vm.name}: {e}")
            return VMResult(
                vm_id    = vm.vm_id,
                name     = vm.name,
                server   = vm.server,
                vnc_port = vm.vnc_port,
                status   = "error",
                error    = str(e),
            )

    # ------------------------------------------------------------------
    # Borrar slice
    # ------------------------------------------------------------------

    def delete_slice(
        self,
        slice_id: str,
        vlan_id:  int,
        cidr:     str,
        vms:      list[dict],   # lista de dicts con name, server, vnc_port
    ) -> bool:
        """
        Destruye un slice completo.

        Parámetros:
            slice_id : ID del slice
            vlan_id  : VLAN ID del slice
            cidr     : red del slice
            vms      : lista de dicts con name y server de cada VM

        Retorna True si el borrado fue exitoso.
        """
        logger.info(f"[LinuxDriver] Borrando slice {slice_id}")
        success = True

        try:
            # Paso 1: borrar VMs
            for vm in vms:
                try:
                    with self._get_client(vm["server"]) as client:
                        mgr = VMManager(client)
                        mgr.delete_vm(vm["name"], OVS_BRIDGE)
                except Exception as e:
                    logger.error(f"[LinuxDriver] Error borrando VM {vm['name']}: {e}")
                    success = False

            # Paso 2: limpiar red en headnode
            try:
                with self._get_client("server3") as headnode:
                    net_mgr = NetworkManager(headnode)
                    net_mgr.delete_vlan_network(vlan_id, cidr)
            except Exception as e:
                logger.error(f"[LinuxDriver] Error limpiando red VLAN {vlan_id}: {e}")
                success = False

        except Exception as e:
            logger.error(f"[LinuxDriver] Error general borrando slice {slice_id}: {e}")
            success = False

        if success:
            logger.info(f"[LinuxDriver] Slice {slice_id} borrado exitosamente")
        return success

    # ------------------------------------------------------------------
    # Estado del slice
    # ------------------------------------------------------------------

    def get_slice_status(self, vms: list[dict]) -> list[VMResult]:
        """
        Consulta el estado de todas las VMs de un slice.

        Parámetros:
            vms: lista de dicts con name, server, vm_id, vnc_port
        """
        results = []
        for vm in vms:
            try:
                with self._get_client(vm["server"]) as client:
                    mgr    = VMManager(client)
                    status = mgr.get_vm_status(vm["name"])
                results.append(VMResult(
                    vm_id    = vm["vm_id"],
                    name     = vm["name"],
                    server   = vm["server"],
                    vnc_port = vm.get("vnc_port", 0),
                    status   = status,
                ))
            except Exception as e:
                results.append(VMResult(
                    vm_id    = vm["vm_id"],
                    name     = vm["name"],
                    server   = vm["server"],
                    vnc_port = vm.get("vnc_port", 0),
                    status   = "error",
                    error    = str(e),
                ))
        return results

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    def _rollback(self, request: SliceRequest, created_vms: list):
        """Deshace una creación parcial."""
        logger.warning(
            f"[LinuxDriver] Rollback: borrando {len(created_vms)} VMs creadas"
        )
        for vm in reversed(created_vms):
            try:
                with self._get_client(vm.server) as client:
                    VMManager(client).delete_vm(vm.name, OVS_BRIDGE)
            except Exception as e:
                logger.error(f"[LinuxDriver] Error en rollback de {vm.name}: {e}")

        try:
            with self._get_client("server3") as headnode:
                NetworkManager(headnode).delete_vlan_network(
                    request.vlan_id, request.cidr
                )
        except Exception as e:
            logger.error(f"[LinuxDriver] Error en rollback de red: {e}")

        logger.info(f"[LinuxDriver] Rollback completado")


# ------------------------------------------------------------------
# Funciones de entrada para el RQ Worker
# ------------------------------------------------------------------

def create_slice(request_dict: dict, ssh_config: dict) -> dict:
    """
    Función llamada por el RQ Worker para crear un slice.
    Recibe y retorna dicts serializables para Redis.
    """
    request = SliceRequest(**request_dict)
    driver  = LinuxDriver(**ssh_config)
    result  = driver.create_slice(request)

    return {
        "slice_id": result.slice_id,
        "topology": result.topology,
        "success":  result.success,
        "error":    result.error,
        "vms": [
            {
                "vm_id":    r.vm_id,
                "name":     r.name,
                "server":   r.server,
                "vnc_port": r.vnc_port,
                "status":   r.status,
                "error":    r.error,
            }
            for r in result.vms
        ],
    }


def delete_slice(
    slice_id:   str,
    vlan_id:    int,
    cidr:       str,
    vms:        list[dict],
    ssh_config: dict,
) -> dict:
    """Función llamada por el RQ Worker para borrar un slice."""
    driver  = LinuxDriver(**ssh_config)
    success = driver.delete_slice(slice_id, vlan_id, cidr, vms)
    return {"slice_id": slice_id, "success": success}
