"""
JWT validation for slice_manager.

El JWT lo emite auth_service con los claims:
    sub      → username
    uid      → id numérico
    role     → admin|profesor|coach|alumno
    email
    courses  → list[int]
    exp

Esta función SOLO verifica firma + expiración. La autorización por rol vive
en `rbac.py`.

Backwards compat: tokens emitidos por la versión vieja del auth_service
(que tenían `role=admin` y sin `uid/courses`) siguen siendo válidos; los
campos faltantes se completan con defaults seguros.
"""

from __future__ import annotations

from fastapi import Header, HTTPException
from jose import jwt, JWTError

from .config import settings


def require_token(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Token requerido")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Token inválido o expirado") from exc

    # Defaults defensivos para tokens emitidos por versiones antiguas
    payload.setdefault("role", "admin")  # tokens viejos siempre eran admin
    payload.setdefault("uid", 0)
    payload.setdefault("courses", [])
    payload.setdefault("email", "")

    return payload
