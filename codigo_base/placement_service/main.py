from __future__ import annotations
"""
placement_service/main.py
-------------------------
VM Placement Service :9005
Recibe solicitudes del Worker RQ y decide en qué servidor va cada VM.
"""

import logging
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'slice_manager'))

from database import get_db
from placement import assign_vms, update_server_resources
from models import ServidorFisico

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(
    title="PUCP Cloud — VM Placement",
    version="1.0.0",
    description="R4 — Asigna VMs a servidores físicos",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class PlacementRequest(BaseModel):
    vm_count:  int
    vm_ram_mb: int = 256
    vm_vcpus:  int = 1


class PlacementResponse(BaseModel):
    success: bool
    servers: List[str]
    error:   Optional[str] = None


class ResourceUpdateRequest(BaseModel):
    server_name: str
    vm_ram_mb:   int = 256
    vm_vcpus:    int = 1
    delta:       int = 1   # +1 crear, -1 borrar


@app.get("/health")
def health():
    return {"status": "ok", "service": "placement", "port": 9005}


@app.post("/placement/assign", response_model=PlacementResponse)
def assign(req: PlacementRequest, db: Session = Depends(get_db)):
    """Asigna VMs a servidores usando Round Robin con verificación de capacidad."""
    result = assign_vms(db, req.vm_count, req.vm_ram_mb, req.vm_vcpus)
    if not result.success:
        raise HTTPException(status_code=507, detail=result.error)
    return PlacementResponse(success=True, servers=result.servers)


@app.post("/placement/update")
def update_resources(req: ResourceUpdateRequest, db: Session = Depends(get_db)):
    """Actualiza recursos usados de un servidor tras crear/borrar una VM."""
    update_server_resources(db, req.server_name, req.vm_ram_mb, req.vm_vcpus, req.delta)
    return {"ok": True}


@app.get("/servers")
def list_servers(db: Session = Depends(get_db)):
    """Lista servidores con su capacidad actual."""
    servers = db.query(ServidorFisico).filter(
        ServidorFisico.activo == True,
        ServidorFisico.nombre != "server3",
    ).all()
    return [
        {
            "nombre":       s.nombre,
            "vcpus_total":  s.vcpus_total,
            "ram_total_mb": s.ram_total_mb,
            "vcpus_used":   s.vcpus_used,
            "ram_used_mb":  s.ram_used_mb,
            "vms_activas":  s.vms_activas,
            "vcpus_libre":  s.vcpus_total  - s.vcpus_used,
            "ram_libre_mb": s.ram_total_mb - s.ram_used_mb,
        }
        for s in servers
    ]