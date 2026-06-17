"""
Pydantic schemas para auth_service: peticiones y respuestas HTTP.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field

Rol = Literal["admin", "profesor", "coach", "alumno"]


# ─── Login ──────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=200)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserPublic"


# ─── Usuarios ───────────────────────────────────────────────────────────────
class UserPublic(BaseModel):
    id: int
    username: str
    email: EmailStr
    rol: Rol
    activo: bool
    courses: list[int] = Field(default_factory=list)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=6, max_length=200)
    email: EmailStr
    rol: Rol = "alumno"


class UserUpdateRequest(BaseModel):
    email: Optional[EmailStr] = None
    rol: Optional[Rol] = None
    activo: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=6, max_length=200)


# ─── Cursos ─────────────────────────────────────────────────────────────────
class CursoPublic(BaseModel):
    id: int
    codigo: str
    nombre: str
    profesor_id: Optional[int]
    profesor_username: Optional[str]
    periodo: str
    activo: bool
    alumnos: list[str] = Field(default_factory=list)


class CursoCreateRequest(BaseModel):
    codigo: str = Field(min_length=2, max_length=20)
    nombre: str = Field(min_length=2, max_length=150)
    profesor_username: Optional[str] = None
    periodo: str = "2026-1"


class CursoUpdateRequest(BaseModel):
    nombre: Optional[str] = Field(default=None, min_length=2, max_length=150)
    profesor_username: Optional[str] = None
    periodo: Optional[str] = None
    activo: Optional[bool] = None


class EnrollmentRequest(BaseModel):
    alumno_usernames: list[str] = Field(min_length=1)


LoginResponse.model_rebuild()
