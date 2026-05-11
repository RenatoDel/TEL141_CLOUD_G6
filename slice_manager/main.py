from __future__ import annotations
import uuid
import logging
from typing import List

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from database import get_db
from models import (
    Slice, VM, Job, Topologia, ServidorFisico,
    EstadoSliceEnum, EstadoJobEnum, TipoJobEnum
)
from schemas import (
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

    # Validar que no exista la VLAN
    existing = db.query(Slice).filter(Slice.vlan_id == req.vlan_id).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"VLAN {req.vlan_id} ya está en uso")

    # Validar servers
    if len(req.servers) != req.vm_count:
        raise HTTPException(status_code=400, detail="servers debe tener un elemento por VM")

    if req.topology == "ring" and req.vm_count < 3:
        raise HTTPException(status_code=400, detail="Topología ring requiere mínimo 3 VMs")

    # Crear slice en MySQL
    slice_uid = f"slice-{uuid.uuid4().hex[:8]}"
    new_slice = Slice(
        slice_uid    = slice_uid,
        nombre       = req.nombre,
        usuario_id   = 1,  # admin por ahora — AAA lo reemplazará
        topologia_id = topologia.id,
        vlan_id      = req.vlan_id,
        cidr         = req.cidr,
        estado       = EstadoSliceEnum.creating,
        tiene_internet = req.has_internet,
        tiene_dhcp     = req.has_dhcp,
    )
    db.add(new_slice)
    db.flush()  # obtener new_slice.id sin commit

    # Crear VMs en MySQL
    for i, server_name in enumerate(req.servers):
        servidor = db.query(ServidorFisico).filter(
            ServidorFisico.nombre == server_name
        ).first()
        if not servidor:
            db.rollback()
            raise HTTPException(status_code=400, detail=f"Servidor '{server_name}' no existe")

        vm = VM(
            vm_uid      = f"{slice_uid}-vm{i+1}",
            nombre      = f"{slice_uid}-vm{i+1}",
            slice_id    = new_slice.id,
            servidor_id = servidor.id,
            vnc_port    = req.vnc_start + i,
            estado      = "creating",
        )
        db.add(vm)

    # Crear job en MySQL
    job_uid = f"job-{uuid.uuid4().hex[:8]}"
    job = Job(
        job_uid  = job_uid,
        slice_id = new_slice.id,
        tipo     = TipoJobEnum.create,
        estado   = EstadoJobEnum.queued,
        progreso = {
            "steps": [
                {"label": "Validando request",      "status": "done"},
                {"label": "VM Placement",           "status": "queued"},
                {"label": "Configurando red VLAN",  "status": "queued"},
                {"label": "Creando VMs en cluster", "status": "queued"},
                {"label": "Verificando estado VMs", "status": "queued"},
            ]
        },
    )
    db.add(job)
    db.commit()

    logger.info(f"Slice {slice_uid} creado — job {job_uid} encolado")

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

    logger.info(f"Slice {slice_uid} marcado para borrado — job {job_uid} encolado")
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