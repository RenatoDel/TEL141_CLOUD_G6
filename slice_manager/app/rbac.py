"""
RBAC para slice_manager.

Reglas centrales:

  admin     : ve y actúa sobre todo
  profesor  : ve y actúa sobre slices propios + slices de alumnos en sus cursos.
              Puede crear slices "on behalf of" un alumno de sus cursos
              (campo owner_username en el payload).
  coach     : ve TODO (read-only). No puede crear/editar/borrar/acciones.
  alumno    : ve y actúa solo sobre sus propios slices.

El JWT incluye los claims:
  sub      → username
  uid      → id numérico
  role     → admin|profesor|coach|alumno
  courses  → list[int] de course_ids
              · alumno   → cursos en los que está inscrito
              · profesor → cursos que dicta
              · admin/coach → []
"""

from __future__ import annotations

from typing import Iterable

from fastapi import Depends, HTTPException

from .auth import require_token


# ════════════════════════════════════════════════════════════════════════════
# Constantes
# ════════════════════════════════════════════════════════════════════════════
ROL_ADMIN = "admin"
ROL_PROFESOR = "profesor"
ROL_COACH = "coach"
ROL_ALUMNO = "alumno"

ROLES_VALIDOS = {ROL_ADMIN, ROL_PROFESOR, ROL_COACH, ROL_ALUMNO}
ROLES_READ_ONLY = {ROL_COACH}  # ven pero no actúan


# ════════════════════════════════════════════════════════════════════════════
# Dependencias FastAPI
# ════════════════════════════════════════════════════════════════════════════
def current_user(user: dict = Depends(require_token)) -> dict:
    """Usuario autenticado. Devuelve el payload del JWT enriquecido."""
    role = user.get("role")
    if role not in ROLES_VALIDOS:
        raise HTTPException(
            status_code=403, detail=f"Rol no reconocido en el token: {role!r}"
        )
    user.setdefault("courses", [])
    return user


def require_role(*roles: str):
    """
    Dependency factory: exige que el rol del token esté en la lista permitida.

        @app.post("/x", dependencies=[Depends(require_role("admin","profesor"))])
    """
    allowed = set(roles)

    def _checker(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"Rol insuficiente. Se requiere uno de: {sorted(allowed)}",
            )
        return user

    return _checker


def require_write_access(user: dict = Depends(current_user)) -> dict:
    """
    Cualquier rol salvo los read-only (coach).
    Útil para endpoints de mutación que NO discriminan más allá de eso.
    """
    if user["role"] in ROLES_READ_ONLY:
        raise HTTPException(
            status_code=403,
            detail=f"El rol '{user['role']}' es de solo lectura",
        )
    return user


# ════════════════════════════════════════════════════════════════════════════
# Reglas de visibilidad y acción sobre slices
# ════════════════════════════════════════════════════════════════════════════
def can_view_slice(user: dict, slice_data: dict) -> bool:
    """
    ¿Este usuario puede VER este slice?

    admin/coach  → cualquier slice
    profesor     → slices propios + slices de cursos que dicta
    alumno       → solo sus propios slices
    """
    role = user["role"]
    if role in (ROL_ADMIN, ROL_COACH):
        return True

    owner = slice_data.get("owner_username")
    curso_id = slice_data.get("curso_id")

    if role == ROL_PROFESOR:
        if owner == user["sub"]:
            return True
        if curso_id is not None and curso_id in user.get("courses", []):
            return True
        return False

    # alumno
    return owner == user["sub"]


def can_act_on_slice(user: dict, slice_data: dict) -> bool:
    """
    ¿Este usuario puede MUTAR este slice (crear/editar/borrar/acciones)?

    coach        → nunca (read-only)
    admin        → sí, sobre cualquiera
    profesor     → sobre slices propios + slices de cursos que dicta
    alumno       → solo sobre sus propios slices
    """
    if user["role"] in ROLES_READ_ONLY:
        return False
    return can_view_slice(user, slice_data)


def filter_slices_for_user(user: dict, slices: Iterable[dict]) -> list[dict]:
    """Filtro de listado: aplica can_view_slice a cada elemento."""
    return [s for s in slices if can_view_slice(user, s)]


def assert_can_view(user: dict, slice_data: dict):
    if not can_view_slice(user, slice_data):
        raise HTTPException(status_code=403, detail="No tienes acceso a este slice")


def assert_can_act(user: dict, slice_data: dict):
    if not can_act_on_slice(user, slice_data):
        raise HTTPException(
            status_code=403, detail="No tienes permiso para modificar este slice"
        )


# ════════════════════════════════════════════════════════════════════════════
# Resolución de ownership al crear un slice
# ════════════════════════════════════════════════════════════════════════════
def resolve_owner_for_create(
    user: dict,
    requested_owner: str | None,
    requested_curso_id: int | None,
) -> tuple[str, int | None]:
    """
    Determina (owner_username, curso_id) finales al crear un slice.

    Reglas:
      - Si no se especifica owner, el dueño es el caller.
      - Solo admin y profesor pueden crear slices "on behalf of" otro usuario.
        · admin: para cualquier alumno.
        · profesor: solo para alumnos de sus cursos. El curso_id resultante
          debe ser uno de los que el profesor dicta (si se especifica).
      - Si el caller es alumno e intenta especificar otro owner → 403.
      - coach nunca llega aquí: se bloquea antes en require_write_access.
    """
    role = user["role"]
    caller = user["sub"]

    if not requested_owner:
        # Auto: dueño = caller. Si el caller es alumno y solo está en un curso,
        # podemos sugerir ese curso. Para mantener simple: dejamos curso_id tal cual.
        return (caller, requested_curso_id)

    if requested_owner == caller:
        return (caller, requested_curso_id)

    # Aquí requested_owner != caller → solo admin/profesor pueden
    if role == ROL_ALUMNO:
        raise HTTPException(
            status_code=403,
            detail="Un alumno solo puede crear slices a su propio nombre",
        )

    if role == ROL_PROFESOR:
        # El curso del slice debe ser uno que el profesor dicta
        if requested_curso_id is None:
            raise HTTPException(
                status_code=400,
                detail="Al crear un slice para otro usuario, debes indicar curso_id",
            )
        if requested_curso_id not in user.get("courses", []):
            raise HTTPException(
                status_code=403,
                detail="Solo puedes crear slices en cursos que dictes",
            )
        # No verificamos aquí que el alumno esté inscrito en ese curso
        # (sería un round-trip al auth_service). Lo dejamos como confianza
        # del profesor; si quieres reforzarlo, añadimos una llamada HTTP a
        # auth_service /courses/{codigo} y verificamos.
        return (requested_owner, requested_curso_id)

    # admin: libre
    return (requested_owner, requested_curso_id)
