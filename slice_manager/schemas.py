from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
from enum import Enum


class TopologiaEnum(str, Enum):
    linear = "linear"
    ring   = "ring"

class EstadoSlice(str, Enum):
    creating = "creating"
    running  = "running"
    deleting = "deleting"
    error    = "error"
    deleted  = "deleted"

class EstadoJob(str, Enum):
    queued    = "queued"
    running   = "running"
    completed = "completed"
    failed    = "failed"


# ------------------------------------------------------------------
# Auth
# ------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"


# ------------------------------------------------------------------
# Slice
# ------------------------------------------------------------------

class SliceCreateRequest(BaseModel):
    nombre:       str           = Field(..., example="slice-lab4-001")
    topology:     TopologiaEnum
    vlan_id:      int           = Field(..., ge=1, le=4094)
    cidr:         str           = Field(..., example="192.168.100.0/24")
    vm_count:     int           = Field(..., ge=2, le=10)
    vnc_start:    int           = Field(default=5901)
    has_internet: bool          = False
    has_dhcp:     bool          = False
    dhcp_start:   Optional[str] = None
    dhcp_end:     Optional[str] = None

class VMResponse(BaseModel):
    vm_uid:   str
    nombre:   str
    servidor: str
    vnc_port: int
    estado:   str

    class Config:
        from_attributes = True

    @classmethod
    def from_orm_vm(cls, vm):
        return cls(
            vm_uid   = vm.vm_uid,
            nombre   = vm.nombre,
            servidor = vm.servidor.nombre,
            vnc_port = vm.vnc_port,
            estado   = vm.estado,
        )


class SliceResponse(BaseModel):
    slice_uid:  str
    nombre:     str
    topologia:  str
    vlan_id:    int
    cidr:       str
    estado:     EstadoSlice
    creado_en:  datetime
    vms:        List[VMResponse] = []

    class Config:
        from_attributes = True

    @classmethod
    def from_orm_slice(cls, s):
        return cls(
            slice_uid = s.slice_uid,
            nombre    = s.nombre,
            topologia = s.topologia.nombre,
            vlan_id   = s.vlan_id,
            cidr      = s.cidr,
            estado    = s.estado,
            creado_en = s.creado_en,
            vms       = [VMResponse.from_orm_vm(vm) for vm in s.vms],
        )


# ------------------------------------------------------------------
# Job
# ------------------------------------------------------------------

class JobResponse(BaseModel):
    job_uid:   str
    slice_uid: str
    tipo:      str
    estado:    EstadoJob
    progreso:  Optional[dict] = None
    error:     Optional[str]  = None
    creado_en: datetime

    class Config:
        from_attributes = True