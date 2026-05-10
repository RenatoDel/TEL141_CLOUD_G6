from __future__ import annotations
"""
topologies/ring.py
------------------
Topología anillo para slices en la arquitectura VNRT.

VM1 ── VM2 ── VM3
 └──────────────┘

N VMs → N enlaces (N-1 lineales + 1 de cierre).
Requiere mínimo 3 VMs.

En esta arquitectura el anillo se implementa con la misma VLAN:
todas las VMs del slice se ven entre sí a nivel L2 via el OFS.
La topología "anillo" define el orden lógico de conexión —
las IPs se asignan de forma consecutiva y el primer y último
nodo también se comunican.
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class RingVM:
    """Una VM en la topología anillo."""
    vm_id:    str
    name:     str
    server:   str
    vnc_port: int
    position: int   # posición en el anillo: 0, 1, 2, ... N-1


@dataclass
class RingSlice:
    """Descripción completa de un slice con topología anillo."""
    slice_id:     str
    vlan_id:      int
    cidr:         str
    vms:          list[RingVM]
    has_internet: bool = False
    has_dhcp:     bool = False
    dhcp_start:   str  = ""
    dhcp_end:     str  = ""


def build_ring_slice(
    slice_id:     str,
    vlan_id:      int,
    cidr:         str,
    vm_count:     int,
    servers:      list[str],
    vnc_start:    int  = 5901,
    has_internet: bool = False,
    has_dhcp:     bool = False,
    dhcp_start:   str  = "",
    dhcp_end:     str  = "",
) -> RingSlice:
    """
    Construye la descripción de un slice en anillo.

    Requiere mínimo 3 VMs (un anillo de 2 es un enlace simple).

    Parámetros:
        slice_id  : identificador único del slice
        vlan_id   : VLAN ID asignado (único por slice)
        cidr      : red del slice (ej: 192.168.200.0/24)
        vm_count  : número de VMs (mínimo 3)
        servers   : a qué servidor va cada VM (len = vm_count)
        vnc_start : puerto VNC de la primera VM

    Ejemplo — anillo de 3 VMs en dos servidores:
        slice = build_ring_slice(
            slice_id  = "slice-002",
            vlan_id   = 200,
            cidr      = "192.168.200.0/24",
            vm_count  = 3,
            servers   = ["server1", "server2", "server1"],
            vnc_start = 5904,
        )
    """
    if vm_count < 3:
        raise ValueError(
            f"Topología anillo requiere mínimo 3 VMs, se recibieron {vm_count}"
        )
    if len(servers) != vm_count:
        raise ValueError(
            f"Debes indicar un servidor por VM. "
            f"vm_count={vm_count} pero servers tiene {len(servers)} elementos."
        )

    vms = []
    for i in range(vm_count):
        vm = RingVM(
            vm_id    = f"{slice_id}-vm{i+1}",
            name     = f"{slice_id}-vm{i+1}",
            server   = servers[i],
            vnc_port = vnc_start + i,
            position = i,
        )
        vms.append(vm)
        logger.debug(
            f"[Ring] VM{i+1}: {vm.name} en {vm.server} "
            f"VNC:{vm.vnc_port} posición:{vm.position}"
        )

    logger.info(
        f"[Ring] Slice {slice_id} construido: "
        f"{vm_count} VMs, VLAN {vlan_id}, CIDR {cidr}, "
        f"{vm_count} enlaces (incluye cierre del anillo)"
    )

    return RingSlice(
        slice_id     = slice_id,
        vlan_id      = vlan_id,
        cidr         = cidr,
        vms          = vms,
        has_internet = has_internet,
        has_dhcp     = has_dhcp,
        dhcp_start   = dhcp_start,
        dhcp_end     = dhcp_end,
    )


def get_ring_summary(slice_obj: RingSlice) -> dict:
    """Retorna un resumen de la topología anillo."""
    vms   = slice_obj.vms
    n     = len(vms)

    # Todos los enlaces incluyendo el de cierre
    links = []
    for i in range(n):
        next_i = (i + 1) % n  # el % n hace que el último conecte al primero
        links.append({
            "from":           vms[i].vm_id,
            "to":             vms[next_i].vm_id,
            "is_closing_link": i == n - 1,
        })

    return {
        "topology":         "ring",
        "slice_id":         slice_obj.slice_id,
        "vlan_id":          slice_obj.vlan_id,
        "cidr":             slice_obj.cidr,
        "vm_count":         n,
        "link_count":       n,
        "closing_link": {
            "from": vms[-1].vm_id,
            "to":   vms[0].vm_id,
        },
        "has_internet":     slice_obj.has_internet,
        "has_dhcp":         slice_obj.has_dhcp,
        "vms": [
            {
                "vm_id":    v.vm_id,
                "server":   v.server,
                "vnc_port": v.vnc_port,
                "position": v.position,
            }
            for v in vms
        ],
        "links": links,
    }
