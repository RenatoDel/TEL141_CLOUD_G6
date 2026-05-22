from __future__ import annotations
"""
placement.py
------------
VM Placement — Round Robin con verificación de capacidad.

Algoritmo:
    1. Obtiene lista de servidores activos ordenados por vms_activas
    2. Para cada VM del slice, asigna al servidor con menos carga
       que tenga capacidad disponible
    3. Si ningún servidor tiene capacidad, rechaza el request

Criterios de capacidad (configurables):
    MAX_VMS_PER_SERVER  = máximo de VMs activas por servidor
    MAX_RAM_PERCENT     = porcentaje máximo de RAM a usar
    MAX_VCPU_PERCENT    = porcentaje máximo de vCPUs a usar
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'slice_manager'))

import logging
from dataclasses import dataclass
from typing import List, Optional
from sqlalchemy.orm import Session
from models import ServidorFisico

logger = logging.getLogger(__name__)

# Límites de capacidad — ajustables según SLA
MAX_VMS_PER_SERVER = 10
MAX_RAM_PERCENT    = 0.85   # no usar más del 85% de RAM
MAX_VCPU_PERCENT   = 0.90   # no usar más del 90% de vCPUs

# RAM y vCPUs por defecto de cada VM
VM_RAM_MB  = 256
VM_VCPUS   = 1


@dataclass
class PlacementResult:
    """Resultado del placement para un slice completo."""
    success:  bool
    servers:  List[str]        # servidor asignado a cada VM
    error:    Optional[str] = None


def _has_capacity(server: ServidorFisico, vm_ram_mb: int, vm_vcpus: int) -> bool:
    """Verifica si un servidor tiene capacidad para una VM más."""
    if server.vms_activas >= MAX_VMS_PER_SERVER:
        logger.debug(f"[Placement] {server.nombre} lleno — {server.vms_activas} VMs activas")
        return False

    ram_after  = server.ram_used_mb  + vm_ram_mb
    vcpu_after = server.vcpus_used   + vm_vcpus
    ram_limit  = server.ram_total_mb  * MAX_RAM_PERCENT
    vcpu_limit = server.vcpus_total   * MAX_VCPU_PERCENT

    if ram_after > ram_limit:
        logger.debug(
            f"[Placement] {server.nombre} sin RAM — "
            f"usado={server.ram_used_mb}MB + {vm_ram_mb}MB > límite={ram_limit:.0f}MB"
        )
        return False

    if vcpu_after > vcpu_limit:
        logger.debug(
            f"[Placement] {server.nombre} sin vCPUs — "
            f"usado={server.vcpus_used} + {vm_vcpus} > límite={vcpu_limit:.0f}"
        )
        return False

    return True


def assign_vms(
    db:         Session,
    vm_count:   int,
    vm_ram_mb:  int = VM_RAM_MB,
    vm_vcpus:   int = VM_VCPUS,
) -> PlacementResult:
    """
    Asigna VMs a servidores usando Round Robin con verificación de capacidad.

    Parámetros:
        db        : sesión de SQLAlchemy
        vm_count  : número de VMs a colocar
        vm_ram_mb : RAM requerida por cada VM en MB
        vm_vcpus  : vCPUs requeridos por cada VM

    Retorna PlacementResult con la lista de servidores asignados.
    """
    # Solo servidores de cómputo — server3 es headnode, no corre VMs
    servers = db.query(ServidorFisico).filter(
        ServidorFisico.activo == True,
        ServidorFisico.nombre != "server3",
    ).order_by(ServidorFisico.vms_activas.asc()).all()

    if not servers:
        return PlacementResult(success=False, servers=[], error="No hay servidores disponibles")

    logger.info(
        f"[Placement] Colocando {vm_count} VMs en {len(servers)} servidores — "
        f"estado: {[(s.nombre, s.vms_activas) for s in servers]}"
    )

    assigned      = []
    # contador local para no depender del orden de la DB durante el placement
    local_counts  = {s.nombre: s.vms_activas for s in servers}
    rr_index      = 0   # índice round robin

    for i in range(vm_count):
        placed = False

        # intentar cada servidor en orden round robin
        for attempt in range(len(servers)):
            idx    = (rr_index + attempt) % len(servers)
            server = servers[idx]

            # recalcular capacidad con asignaciones locales ya hechas
            local_ram_used  = server.ram_used_mb  + assigned.count(server.nombre) * vm_ram_mb
            local_vcpu_used = server.vcpus_used   + assigned.count(server.nombre) * vm_vcpus
            local_vms       = local_counts[server.nombre]

            if (local_vms       < MAX_VMS_PER_SERVER and
                local_ram_used  + vm_ram_mb  <= server.ram_total_mb  * MAX_RAM_PERCENT and
                local_vcpu_used + vm_vcpus   <= server.vcpus_total   * MAX_VCPU_PERCENT):

                assigned.append(server.nombre)
                local_counts[server.nombre] += 1
                logger.info(f"[Placement] VM{i+1} → {server.nombre}")
                placed    = True
                rr_index  = (idx + 1) % len(servers)  # avanzar RR
                break

        if not placed:
            return PlacementResult(
                success = False,
                servers = [],
                error   = (
                    f"Sin capacidad para VM{i+1}. "
                    f"Servidores: {[(s.nombre, s.vms_activas) for s in servers]}"
                ),
            )

    logger.info(f"[Placement] Asignación completada: {assigned}")
    return PlacementResult(success=True, servers=assigned)


def update_server_resources(
    db:        Session,
    server_name: str,
    vm_ram_mb:   int = VM_RAM_MB,
    vm_vcpus:    int = VM_VCPUS,
    delta:       int = 1,   # +1 al crear, -1 al borrar
):
    """
    Actualiza los recursos usados de un servidor en MySQL.
    Llamar después de crear o borrar una VM exitosamente.
    """
    server = db.query(ServidorFisico).filter(
        ServidorFisico.nombre == server_name
    ).first()

    if not server:
        logger.warning(f"[Placement] Servidor {server_name} no encontrado")
        return

    server.vms_activas  = max(0, server.vms_activas  + delta)
    server.ram_used_mb  = max(0, server.ram_used_mb  + delta * vm_ram_mb)
    server.vcpus_used   = max(0, server.vcpus_used   + delta * vm_vcpus)
    db.commit()

    logger.info(
        f"[Placement] {server_name} actualizado — "
        f"vms={server.vms_activas} ram={server.ram_used_mb}MB vcpus={server.vcpus_used}"
    )