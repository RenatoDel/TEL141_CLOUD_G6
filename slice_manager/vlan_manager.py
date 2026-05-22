from __future__ import annotations
"""
vlan_manager.py
---------------
Gestión automática de VLAN IDs y puertos VNC.
El usuario nunca elige estos valores — el sistema los asigna.
"""

import logging
from datetime import datetime
from sqlalchemy.orm import Session
from fastapi import HTTPException
from models import VlanPool, VM

logger = logging.getLogger(__name__)


def assign_vlan(db: Session, slice_id: int) -> int:
    """
    Asigna el siguiente VLAN ID disponible de forma atómica.
    Usa SELECT FOR UPDATE para evitar race conditions.
    """
    pool = db.query(VlanPool).filter(
        VlanPool.en_uso == False
    ).order_by(VlanPool.vlan_id.asc()).with_for_update().first()

    if not pool:
        raise HTTPException(status_code=507, detail="No hay VLANs disponibles")

    pool.en_uso       = True
    pool.slice_id     = slice_id
    pool.reservado_en = datetime.utcnow()
    db.flush()

    logger.info(f"[VlanManager] VLAN {pool.vlan_id} asignada al slice {slice_id}")
    return pool.vlan_id


def release_vlan(db: Session, vlan_id: int):
    """
    Libera un VLAN ID cuando se borra un slice.
    """
    pool = db.query(VlanPool).filter(VlanPool.vlan_id == vlan_id).first()
    if pool:
        pool.en_uso       = False
        pool.slice_id     = None
        pool.reservado_en = None
        db.flush()
        logger.info(f"[VlanManager] VLAN {vlan_id} liberada")


def get_next_vnc_port(db: Session) -> int:
    """
    Retorna el siguiente puerto VNC disponible.
    Empieza en 5901 y va incrementando.
    """
    last_vm = db.query(VM).order_by(VM.vnc_port.desc()).first()
    if not last_vm:
        return 5901
    next_port = last_vm.vnc_port + 1
    logger.info(f"[VlanManager] Puerto VNC asignado: {next_port}")
    return next_port


def cidr_from_vlan(vlan_id: int) -> str:
    """
    Genera el CIDR automáticamente desde el VLAN ID.
    VLAN 100 → 192.168.100.0/24
    VLAN 200 → 192.168.200.0/24
    VLAN 856 → 10.856... — para VLANs > 255 usa rango 10.x.x.0/24
    """
    if vlan_id <= 255:
        return f"192.168.{vlan_id}.0/24"
    else:
        # para VLANs > 255: 10.0.{vlan_id - 256}.0/24
        third_octet = vlan_id - 256
        return f"10.0.{third_octet}.0/24"