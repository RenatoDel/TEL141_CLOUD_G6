from __future__ import annotations
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime,
    Enum, Text, JSON, ForeignKey
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base
import enum


class RolEnum(str, enum.Enum):
    admin   = "admin"
    usuario = "usuario"

class EstadoSliceEnum(str, enum.Enum):
    creating = "creating"
    running  = "running"
    deleting = "deleting"
    error    = "error"
    deleted  = "deleted"

class EstadoVMEnum(str, enum.Enum):
    creating = "creating"
    running  = "running"
    stopped  = "stopped"
    error    = "error"
    deleted  = "deleted"

class EstadoJobEnum(str, enum.Enum):
    queued    = "queued"
    running   = "running"
    completed = "completed"
    failed    = "failed"

class TipoJobEnum(str, enum.Enum):
    create = "create"
    delete = "delete"


class Usuario(Base):
    __tablename__ = "usuario"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    username      = Column(String(50),  nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    email         = Column(String(100), nullable=False, unique=True)
    rol           = Column(Enum(RolEnum), nullable=False, default=RolEnum.usuario)
    activo        = Column(Boolean, nullable=False, default=True)
    creado_en     = Column(DateTime, server_default=func.now())

    slices = relationship("Slice", back_populates="usuario")
    tokens = relationship("TokenJWT", back_populates="usuario")


class TokenJWT(Base):
    __tablename__ = "token_jwt"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    usuario_id = Column(Integer, ForeignKey("usuario.id", ondelete="CASCADE"), nullable=False)
    token      = Column(String(512), nullable=False, unique=True)
    expira_en  = Column(DateTime, nullable=False)
    creado_en  = Column(DateTime, server_default=func.now())

    usuario = relationship("Usuario", back_populates="tokens")


class Topologia(Base):
    __tablename__ = "topologia"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    nombre      = Column(String(50),  nullable=False, unique=True)
    descripcion = Column(String(255))

    slices = relationship("Slice", back_populates="topologia")


class ServidorFisico(Base):
    __tablename__ = "servidor_fisico"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    nombre       = Column(String(50), nullable=False, unique=True)
    ip_interna   = Column(String(15), nullable=False, unique=True)
    vcpus_total  = Column(Integer, nullable=False, default=4)
    ram_total_mb = Column(Integer, nullable=False, default=8192)
    activo       = Column(Boolean, nullable=False, default=True)
    vcpus_used   = Column(Integer, nullable=False, default=0)
    ram_used_mb  = Column(Integer, nullable=False, default=0)
    vms_activas  = Column(Integer, nullable=False, default=0)

    vms = relationship("VM", back_populates="servidor")

class Slice(Base):
    __tablename__ = "slice"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    slice_uid      = Column(String(100), nullable=False, unique=True)
    nombre         = Column(String(100), nullable=False)
    usuario_id     = Column(Integer, ForeignKey("usuario.id"), nullable=False)
    topologia_id   = Column(Integer, ForeignKey("topologia.id"), nullable=False)
    vlan_id        = Column(Integer, nullable=False, unique=True)
    cidr           = Column(String(18), nullable=False)
    estado         = Column(Enum(EstadoSliceEnum), nullable=False, default=EstadoSliceEnum.creating)
    tiene_internet = Column(Boolean, nullable=False, default=False)
    tiene_dhcp     = Column(Boolean, nullable=False, default=False)
    creado_en      = Column(DateTime, server_default=func.now())
    actualizado_en = Column(DateTime, server_default=func.now(), onupdate=func.now())

    usuario  = relationship("Usuario",  back_populates="slices")
    topologia= relationship("Topologia",back_populates="slices")
    vms      = relationship("VM",       back_populates="slice", cascade="all, delete-orphan")
    enlaces  = relationship("Enlace",   back_populates="slice", cascade="all, delete-orphan")
    jobs     = relationship("Job",      back_populates="slice", cascade="all, delete-orphan")


class VM(Base):
    __tablename__ = "vm"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    vm_uid      = Column(String(100), nullable=False, unique=True)
    nombre      = Column(String(100), nullable=False)
    slice_id    = Column(Integer, ForeignKey("slice.id",  ondelete="CASCADE"), nullable=False)
    servidor_id = Column(Integer, ForeignKey("servidor_fisico.id"), nullable=False)
    vnc_port    = Column(Integer, nullable=False)
    ram_mb      = Column(Integer, nullable=False, default=256)
    vcpus       = Column(Integer, nullable=False, default=1)
    estado      = Column(Enum(EstadoVMEnum), nullable=False, default=EstadoVMEnum.creating)
    creado_en   = Column(DateTime, server_default=func.now())

    slice   = relationship("Slice",         back_populates="vms")
    servidor= relationship("ServidorFisico",back_populates="vms")


class Enlace(Base):
    __tablename__ = "enlace"

    id       = Column(Integer, primary_key=True, autoincrement=True)
    slice_id = Column(Integer, ForeignKey("slice.id", ondelete="CASCADE"), nullable=False)
    vm_src   = Column(Integer, ForeignKey("vm.id",    ondelete="CASCADE"), nullable=False)
    vm_dst   = Column(Integer, ForeignKey("vm.id",    ondelete="CASCADE"), nullable=False)

    slice = relationship("Slice", back_populates="enlaces")


class Job(Base):
    __tablename__ = "job"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    job_uid        = Column(String(100), nullable=False, unique=True)
    slice_id       = Column(Integer, ForeignKey("slice.id", ondelete="CASCADE"), nullable=False)
    tipo           = Column(Enum(TipoJobEnum),   nullable=False)
    estado         = Column(Enum(EstadoJobEnum), nullable=False, default=EstadoJobEnum.queued)
    progreso       = Column(JSON)
    error          = Column(Text)
    creado_en      = Column(DateTime, server_default=func.now())
    actualizado_en = Column(DateTime, server_default=func.now(), onupdate=func.now())

    slice = relationship("Slice", back_populates="jobs")

class VlanPool(Base):
    __tablename__ = "vlan_pool"

    vlan_id      = Column(Integer, primary_key=True)
    en_uso       = Column(Boolean, nullable=False, default=False)
    slice_id     = Column(Integer, ForeignKey("slice.id", ondelete="SET NULL"), nullable=True)
    reservado_en = Column(DateTime, nullable=True)        