from __future__ import annotations
import uuid
import logging
from typing import List
import httpx

import redis
from rq import Queue

PLACEMENT_URL = "http://localhost:9005"
redis_conn = redis.from_url("redis://localhost:6379")
job_queue  = Queue("slice_jobs", connection=redis_conn)

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from codigo_base.slice_manager.vlan_manager import assign_vlan, release_vlan, get_next_vnc_port, cidr_from_vlan


from codigo_base.slice_manager.database import get_db
from codigo_base.slice_manager.models import (
    Slice, VM, Job, Topologia, ServidorFisico,
    EstadoSliceEnum, EstadoJobEnum, TipoJobEnum
)
from codigo_base.slice_manager.schemas import (
    SliceCreateRequest, SliceResponse, VMResponse,
    JobResponse, LoginRequest, TokenResponse
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(
    title="PUCP Cloud — Slice Manager",
    version="1.0.0",
    description="R1C — Gestor de despliegue de slices",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------
# Health
# ------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "service": "slice_manager", "port": 9002}


# ------------------------------------------------------------------
# Slices
# ------------------------------------------------------------------

@app.post("/slices", status_code=status.HTTP_202_ACCEPTED)
def create_slice(req: SliceCreateRequest, db: Session = Depends(get_db)):
    # Validar topología
    topologia = db.query(Topologia).filter(Topologia.nombre == req.topology).first()
    if not topologia:
        raise HTTPException(status_code=400, detail=f"Topología '{req.topology}' no existe")

    if req.topology == "ring" and req.vm_count < 3:
        raise HTTPException(status_code=400, detail="Topología ring requiere mínimo 3 VMs")

    # VM Placement automático
    try:
        placement_resp = httpx.post(
            f"{PLACEMENT_URL}/placement/assign",
            json={"vm_count": req.vm_count},
            timeout=10,
        )
        placement_resp.raise_for_status()
        servers_assigned = placement_resp.json()["servers"]
    except httpx.HTTPStatusError as e:
        detail = e.response.json().get("detail", "Sin capacidad")
        raise HTTPException(status_code=507, detail=detail)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Placement Service no disponible: {e}")

    # Crear slice en MySQL
    slice_uid = f"slice-{uuid.uuid4().hex[:8]}"
    new_slice = Slice(
        slice_uid      = slice_uid,
        nombre         = req.nombre,
        usuario_id     = 1,
        topologia_id   = topologia.id,
        vlan_id        = 0,  # temporal — se asigna abajo
        cidr           = "",  # temporal
        estado         = EstadoSliceEnum.creating,
        tiene_internet = any(v.tiene_public for v in req.vms_config) if req.vms_config else False,
        tiene_dhcp     = False,
    )
    db.add(new_slice)
    db.flush()

    # Asignar VLAN y CIDR automáticamente
    vlan_id = assign_vlan(db, new_slice.id)
    cidr    = cidr_from_vlan(vlan_id)
    new_slice.vlan_id = vlan_id
    new_slice.cidr    = cidr

    # Asignar VMs con VNC automático
    vnc_start = get_next_vnc_port(db)
    for i, server_name in enumerate(servers_assigned):
        servidor = db.query(ServidorFisico).filter(
            ServidorFisico.nombre == server_name
        ).first()

        vm_cfg = req.vms_config[i] if req.vms_config and i < len(req.vms_config) else None
        vm = VM(
            vm_uid      = f"{slice_uid}-vm{i+1}",
            nombre      = f"{slice_uid}-vm{i+1}",
            slice_id    = new_slice.id,
            servidor_id = servidor.id,
            vnc_port    = vnc_start + i,
            ram_mb      = vm_cfg.ram_mb if vm_cfg else 512,
            vcpus       = 1,
            estado      = "creating",
        )
        db.add(vm)

    # Crear job
    job_uid = f"job-{uuid.uuid4().hex[:8]}"
    job = Job(
        job_uid  = job_uid,
        slice_id = new_slice.id,
        tipo     = TipoJobEnum.create,
        estado   = EstadoJobEnum.queued,
        progreso = {
            "steps": [
                {"label": "Validando request",      "status": "done"},
                {"label": "VM Placement",           "status": "done",
                 "detail": f"Asignado: {servers_assigned}"},
                {"label": "Configurando red VLAN",  "status": "queued"},
                {"label": "Creando VMs en cluster", "status": "queued"},
                {"label": "Verificando estado VMs", "status": "queued"},
            ]
        },
    )
    db.add(job)
    db.commit()

    from codigo_base.slice_manager.worker import ejecutar_create_slice
    job_queue.enqueue(ejecutar_create_slice, job_uid)

    logger.info(f"Slice {slice_uid} — VLAN={vlan_id} CIDR={cidr} placement={servers_assigned} — job {job_uid}")
    return {"job_uid": job_uid, "slice_uid": slice_uid}


@app.get("/slices")
def list_slices(db: Session = Depends(get_db)):
    slices = db.query(Slice).filter(
        Slice.estado != EstadoSliceEnum.deleted
    ).all()
    return [SliceResponse.from_orm_slice(s) for s in slices]


@app.get("/slices/{slice_uid}")
def get_slice(slice_uid: str, db: Session = Depends(get_db)):
    s = db.query(Slice).filter(Slice.slice_uid == slice_uid).first()
    if not s:
        raise HTTPException(status_code=404, detail="Slice no encontrado")
    return SliceResponse.from_orm_slice(s)


@app.delete("/slices/{slice_uid}", status_code=status.HTTP_202_ACCEPTED)
def delete_slice(slice_uid: str, db: Session = Depends(get_db)):
    s = db.query(Slice).filter(Slice.slice_uid == slice_uid).first()
    if not s:
        raise HTTPException(status_code=404, detail="Slice no encontrado")

    s.estado = EstadoSliceEnum.deleting

    # Liberar VLAN
    release_vlan(db, s.vlan_id)

    job_uid = f"job-{uuid.uuid4().hex[:8]}"
    job = Job(
        job_uid  = job_uid,
        slice_id = s.id,
        tipo     = TipoJobEnum.delete,
        estado   = EstadoJobEnum.queued,
        progreso = {"steps": []},
    )
    db.add(job)
    db.commit()

    from codigo_base.slice_manager.worker import ejecutar_delete_slice
    job_queue.enqueue(ejecutar_delete_slice, job_uid)

    logger.info(f"Slice {slice_uid} marcado para borrado — VLAN {s.vlan_id} liberada — job {job_uid}")
    return {"job_uid": job_uid, "slice_uid": slice_uid}


# ------------------------------------------------------------------
# Jobs
# ------------------------------------------------------------------

@app.get("/jobs/{job_uid}", response_model=JobResponse)
def get_job(job_uid: str, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.job_uid == job_uid).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job no encontrado")

    return JobResponse(
        job_uid   = job.job_uid,
        slice_uid = job.slice.slice_uid,
        tipo      = job.tipo,
        estado    = job.estado,
        progreso  = job.progreso,
        error     = job.error,
        creado_en = job.creado_en,
    )


# ------------------------------------------------------------------
# Servidores
# ------------------------------------------------------------------

@app.get("/servers")
def list_servers(db: Session = Depends(get_db)):
    servers = db.query(ServidorFisico).filter(ServidorFisico.activo == True).all()
    return [
        {
            "id":           s.id,
            "nombre":       s.nombre,
            "ip_interna":   s.ip_interna,
            "vcpus_total":  s.vcpus_total,
            "ram_total_mb": s.ram_total_mb,
        }
        for s in servers
    ]