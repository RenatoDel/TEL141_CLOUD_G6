"""
RBAC para slice_manager.

Reglas centrales:

  admin     : ve y actúa sobre todo.
  profesor  : ve y actúa sobre slices propios + slices de alumnos en sus cursos.
              Puede crear slices "on behalf of" un alumno de sus cursos
              (campo owner_username en el payload).
  coach     : read-only, filtrado por sus cursos asignados.
              Ve slices de cursos donde está asignado como coach.
              No puede crear/editar/borrar/acciones.
  alumno    : ve solo sus propios slices. NO PUEDE CREAR slices
              (solo el profesor del curso puede crearlos a su nombre).

El JWT incluye los claims:
  sub      → username
  uid      → id numérico
  role     → admin|profesor|coach|alumno
  courses  → list[int] de course_ids
              · alumno   → cursos en los que está inscrito
              · profesor → cursos que dicta
              · coach    → cursos que audita (M:N curso_coach)
              · admin    → []
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

    admin        → cualquier slice
    profesor     → slices propios + slices de cursos que dicta
    coach        → slices de cursos asignados (M:N curso_coach)
    alumno       → solo sus propios slices
    """
    role = user["role"]
    if role == ROL_ADMIN:
        return True

    owner = slice_data.get("owner_username")
    curso_id = slice_data.get("curso_id")
    user_courses = user.get("courses", [])

    if role == ROL_PROFESOR:
        if owner == user["sub"]:
            return True
        if curso_id is not None and curso_id in user_courses:
            return True
        return False

    if role == ROL_COACH:
        # Coach SOLO ve slices de cursos que audita. Si el slice no tiene
        # curso_id asignado, no es visible para coaches.
        if curso_id is not None and curso_id in user_courses:
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
      - alumno: NUNCA puede crear slices (ni para sí mismo).
        Solo el profesor del curso los crea a su nombre.
      - coach: bloqueado antes en require_write_access (read-only).
      - Si no se especifica owner, el dueño es el caller.
      - admin/profesor pueden crear "on behalf of" otro usuario:
        · admin: para cualquier alumno.
        · profesor: solo para alumnos de sus cursos. El curso_id resultante
          debe ser uno de los que el profesor dicta.
    """
    role = user["role"]
    caller = user["sub"]

    # Alumno bloqueado completamente para crear slices.
    if role == ROL_ALUMNO:
        raise HTTPException(
            status_code=403,
            detail=(
                "Los alumnos no pueden crear slices. "
                "Pide al profesor de tu curso que cree uno a tu nombre."
            ),
        )

    if not requested_owner:
        # Auto: dueño = caller (admin o profesor creando para sí mismo).
        return (caller, requested_curso_id)

    if requested_owner == caller:
        return (caller, requested_curso_id)

    # requested_owner != caller → solo admin/profesor pueden
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
        return (requested_owner, requested_curso_id)

    # admin: libre
    return (requested_owner, requested_curso_id)
