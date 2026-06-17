"""
bcrypt password hashing + JWT minting/decoding.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import jwt, JWTError

JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-production")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_TTL_HOURS = int(os.getenv("JWT_TTL_HOURS", "12"))


# ─── Passwords ──────────────────────────────────────────────────────────────
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ─── JWT ────────────────────────────────────────────────────────────────────
def mint_token(
    *,
    username: str,
    user_id: int,
    role: str,
    email: str,
    course_ids: list[int],
) -> str:
    """
    Emite un JWT firmado.

    Claims incluidos:
      sub       : username (compatibilidad con código existente)
      uid       : id numérico del usuario
      role      : admin | profesor | coach | alumno
      email     : email del usuario
      courses   : lista de IDs de cursos
                    - alumno   → cursos en los que está inscrito
                    - profesor → cursos que dicta
                    - admin/coach → [] (acceso transversal)
      exp       : timestamp de expiración (UNIX)
    """
    exp = datetime.now(timezone.utc) + timedelta(hours=JWT_TTL_HOURS)
    payload = {
        "sub": username,
        "uid": user_id,
        "role": role,
        "email": email,
        "courses": course_ids,
        "exp": exp,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Devuelve el payload. Lanza JWTError si es inválido o expiró."""
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
