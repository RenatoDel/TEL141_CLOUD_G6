from __future__ import annotations
"""
topologies/linear.py
--------------------
Topología lineal para slices en la arquitectura VNRT.

VM1 ── VM2 ── VM3 ── VM4
N VMs → N-1 enlaces → cada par de VMs conectadas en la misma VLAN.

Cómo funciona el aislamiento por VLAN en esta arquitectura:
- Cada slice tiene una VLAN ID asignada (ej: 100)
- Las VMs del slice se conectan al OVS br-int de sus servidores con ese tag
- El OFS conecta todos los servidores — el tráfico con ese tag VLAN
  viaja entre servidores de forma aislada del resto

Para la topología lineal:
- Todas las VMs del mismo slice usan la misma VLAN ID
- La "linealidad" la define la asignación de IPs manual o DHCP
- El bridge OVS con el tag VLAN asegura que solo se ven entre ellas
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class LinearVM:
    """Una VM en la topología lineal."""
    vm_id:    str   # identificador único: slice_id-vm_N
    name:     str   # nombre legible
    server:   str   # "server1" o "server2"
    vnc_port: int   # puerto VNC asignado
    position: int   # posición en la cadena: 0, 1, 2, ...


@dataclass
class LinearSlice:
    """Descripción completa de un slice con topología lineal."""
    slice_id:   str
    vlan_id:    int
    cidr:       str
    vms:        list[LinearVM]
    has_internet: bool = False
    has_dhcp:     bool = False
    dhcp_start:   str  = ""
    dhcp_end:     str  = ""


def build_linear_slice(
    slice_id:     str,
    vlan_id:      int,
    cidr:         str,
    vm_count:     int,
    servers:      list[str],    # lista de servidores: ["server1", "server1", "server2"]
    vnc_start:    int = 5901,
    has_internet: bool = False,
    has_dhcp:     bool = False,
    dhcp_start:   str  = "",
    dhcp_end:     str  = "",
) -> LinearSlice:
    """
    Construye la descripción de un slice lineal.

    Parámetros:
        slice_id  : identificador único del slice
        vlan_id   : VLAN ID asignado (único por slice)
        cidr      : red del slice (ej: 192.168.100.0/24)
        vm_count  : número de VMs
        servers   : a qué servidor va cada VM (len = vm_count)
        vnc_start : puerto VNC de la primera VM
        has_internet: si las VMs tienen salida a internet
        has_dhcp  : si hay DHCP automático
        dhcp_start: primera IP del rango DHCP
        dhcp_end  : última IP del rango DHCP

    Ejemplo:
        slice = build_linear_slice(
            slice_id  = "slice-001",
            vlan_id   = 100,
            cidr      = "192.168.100.0/24",
            vm_count  = 3,
            servers   = ["server1", "server1", "server2"],
            vnc_start = 5901,
        )
    """
    if len(servers) != vm_count:
        raise ValueError(
            f"Debes indicar un servidor por VM. "
            f"vm_count={vm_count} pero servers tiene {len(servers)} elementos."
        )

    vms = []
    for i in range(vm_count):
        vm = LinearVM(
            vm_id    = f"{slice_id}-vm{i+1}",
            name     = f"{slice_id}-vm{i+1}",
            server   = servers[i],
            vnc_port = vnc_start + i,
            position = i,
        )
        vms.append(vm)
        logger.debug(
            f"[Linear] VM{i+1}: {vm.name} en {vm.server} "
            f"VNC:{vm.vnc_port} posición:{vm.position}"
        )

    logger.info(
        f"[Linear] Slice {slice_id} construido: "
        f"{vm_count} VMs, VLAN {vlan_id}, CIDR {cidr}"
    )

    return LinearSlice(
        slice_id     = slice_id,
        vlan_id      = vlan_id,
        cidr         = cidr,
        vms          = vms,
        has_internet = has_internet,
        has_dhcp     = has_dhcp,
        dhcp_start   = dhcp_start,
        dhcp_end     = dhcp_end,
    )


def get_linear_summary(slice_obj: LinearSlice) -> dict:
    """Retorna un resumen de la topología para logging y documentación."""
    return {
        "topology":     "linear",
        "slice_id":     slice_obj.slice_id,
        "vlan_id":      slice_obj.vlan_id,
        "cidr":         slice_obj.cidr,
        "vm_count":     len(slice_obj.vms),
        "link_count":   len(slice_obj.vms) - 1,
        "has_internet": slice_obj.has_internet,
        "has_dhcp":     slice_obj.has_dhcp,
        "vms": [
            {
                "vm_id":    v.vm_id,
                "server":   v.server,
                "vnc_port": v.vnc_port,
                "position": v.position,
            }
            for v in slice_obj.vms
        ],
        "links": [
            {
                "from": slice_obj.vms[i].vm_id,
                "to":   slice_obj.vms[i+1].vm_id,
            }
            for i in range(len(slice_obj.vms) - 1)
        ],
    }
