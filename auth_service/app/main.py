"""
PUCP Auth Service — v0.2

Endpoints:
  POST   /login                              Autenticación (público)
  GET    /me                                 Datos del usuario actual

  GET    /users                              Listar usuarios            (admin)
  POST   /users                              Crear usuario              (admin)
  GET    /users/{username}                   Detalle de usuario         (admin)
  PATCH  /users/{username}                   Actualizar usuario         (admin)
  DELETE /users/{username}                   Borrar (soft) usuario      (admin)

  GET    /courses                            Listar cursos              (todos)
  POST   /courses                            Crear curso                (admin)
  GET    /courses/{codigo}                   Detalle de curso           (todos)
  PATCH  /courses/{codigo}                   Actualizar curso           (admin/profesor dueño)
  DELETE /courses/{codigo}                   Borrar (soft) curso        (admin)
  POST   /courses/{codigo}/members           Inscribir alumnos          (admin/profesor dueño)
  DELETE /courses/{codigo}/members/{user}    Desinscribir alumno        (admin/profesor dueño)

  GET    /health                             Liveness probe
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from jose import JWTError

from .db import get_conn
from .models import (
    CoachAssignmentRequest,
    CursoCreateRequest,
    CursoPublic,
    CursoUpdateRequest,
    EnrollmentRequest,
    LoginRequest,
    LoginResponse,
    Rol,
    UserCreateRequest,
    UserPublic,
    UserUpdateRequest,
)
from .security import decode_token, hash_password, mint_token, verify_password

app = FastAPI(title="PUCP Auth Service", version="0.2.0")


# ════════════════════════════════════════════════════════════════════════════
# Dependencias de autenticación
# ════════════════════════════════════════════════════════════════════════════
def _bearer_payload(authorization: Optional[str]) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Token requerido")
    token = authorization.split(" ", 1)[1].strip()
    try:
        return decode_token(token)
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Token inválido o expirado") from exc


def current_user(authorization: Optional[str] = Header(default=None)) -> dict:
    """Cualquier usuario autenticado."""
    return _bearer_payload(authorization)


def require_role(*roles: Rol):
    """
    Dependency factory: exige que el rol del token esté en la lista permitida.

        @app.get("/admin-only", dependencies=[Depends(require_role("admin"))])
    """
    def _checker(user: dict = Depends(current_user)) -> dict:
        if user.get("role") not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"Rol insuficiente. Se requiere uno de: {list(roles)}",
            )
        return user
    return _checker


# ════════════════════════════════════════════════════════════════════════════
# Helpers de DB
# ════════════════════════════════════════════════════════════════════════════
def _course_ids_for_user(cur, user_id: int, rol: str) -> list[int]:
    """
    Devuelve los IDs de cursos que aplican según el rol:
      - alumno   → cursos en los que está inscrito
      - profesor → cursos que dicta
      - coach    → cursos que audita (M:N en curso_coach)
      - admin    → []  (acceso transversal, no se filtra por curso)
    """
    if rol == "alumno":
        cur.execute(
            "SELECT curso_id FROM curso_alumno WHERE alumno_id=%s",
            (user_id,),
        )
        return [r["curso_id"] for r in cur.fetchall()]
    if rol == "profesor":
        cur.execute(
            "SELECT id FROM curso WHERE profesor_id=%s AND activo=1",
            (user_id,),
        )
        return [r["id"] for r in cur.fetchall()]
    if rol == "coach":
        cur.execute(
            "SELECT cc.curso_id FROM curso_coach cc "
            "JOIN curso c ON c.id = cc.curso_id "
            "WHERE cc.coach_id=%s AND c.activo=1",
            (user_id,),
        )
        return [r["curso_id"] for r in cur.fetchall()]
    return []


def _user_by_username(cur, username: str) -> Optional[dict]:
    cur.execute(
        "SELECT id, username, password_hash, email, rol, activo "
        "FROM usuario WHERE username=%s",
        (username,),
    )
    return cur.fetchone()


def _user_to_public(cur, row: dict) -> UserPublic:
    courses = _course_ids_for_user(cur, row["id"], row["rol"])
    return UserPublic(
        id=row["id"],
        username=row["username"],
        email=row["email"],
        rol=row["rol"],
        activo=bool(row["activo"]),
        courses=courses,
    )


# ════════════════════════════════════════════════════════════════════════════
# Health
# ════════════════════════════════════════════════════════════════════════════
@app.get("/health")
def health():
    return {"status": "ok"}


# ════════════════════════════════════════════════════════════════════════════
# Login + /me
# ════════════════════════════════════════════════════════════════════════════
@app.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest):
    with get_conn() as conn:
        with conn.cursor() as cur:
            row = _user_by_username(cur, payload.username)
            if not row or not row["activo"]:
                raise HTTPException(status_code=401, detail="Credenciales inválidas")
            if not verify_password(payload.password, row["password_hash"]):
                raise HTTPException(status_code=401, detail="Credenciales inválidas")

            courses = _course_ids_for_user(cur, row["id"], row["rol"])

    token = mint_token(
        username=row["username"],
        user_id=row["id"],
        role=row["rol"],
        email=row["email"],
        course_ids=courses,
    )
    return LoginResponse(
        access_token=token,
        user=UserPublic(
            id=row["id"],
            username=row["username"],
            email=row["email"],
            rol=row["rol"],
            activo=bool(row["activo"]),
            courses=courses,
        ),
    )


@app.get("/me", response_model=UserPublic)
def me(user: dict = Depends(current_user)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            row = _user_by_username(cur, user["sub"])
            if not row:
                raise HTTPException(status_code=404, detail="Usuario no encontrado")
            return _user_to_public(cur, row)


# ════════════════════════════════════════════════════════════════════════════
# Usuarios (solo admin)
# ════════════════════════════════════════════════════════════════════════════
@app.get("/users", response_model=list[UserPublic])
def list_users(_admin=Depends(require_role("admin"))):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, email, rol, activo FROM usuario ORDER BY id"
            )
            rows = cur.fetchall()
            return [_user_to_public(cur, r) for r in rows]


@app.post("/users", response_model=UserPublic, status_code=201)
def create_user(
    payload: UserCreateRequest,
    _admin=Depends(require_role("admin")),
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM usuario WHERE username=%s OR email=%s",
                (payload.username, payload.email),
            )
            if cur.fetchone():
                raise HTTPException(
                    status_code=409,
                    detail="Username o email ya registrado",
                )
            cur.execute(
                "INSERT INTO usuario (username, password_hash, email, rol) "
                "VALUES (%s, %s, %s, %s)",
                (
                    payload.username,
                    hash_password(payload.password),
                    payload.email,
                    payload.rol,
                ),
            )
            new_id = cur.lastrowid
            cur.execute(
                "SELECT id, username, email, rol, activo FROM usuario WHERE id=%s",
                (new_id,),
            )
            row = cur.fetchone()
            return _user_to_public(cur, row)


@app.get("/users/{username}", response_model=UserPublic)
def get_user(username: str, _admin=Depends(require_role("admin"))):
    with get_conn() as conn:
        with conn.cursor() as cur:
            row = _user_by_username(cur, username)
            if not row:
                raise HTTPException(status_code=404, detail="Usuario no encontrado")
            return _user_to_public(cur, row)


@app.patch("/users/{username}", response_model=UserPublic)
def update_user(
    username: str,
    payload: UserUpdateRequest,
    _admin=Depends(require_role("admin")),
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            row = _user_by_username(cur, username)
            if not row:
                raise HTTPException(status_code=404, detail="Usuario no encontrado")

            fields, values = [], []
            if payload.email is not None:
                fields.append("email=%s")
                values.append(payload.email)
            if payload.rol is not None:
                fields.append("rol=%s")
                values.append(payload.rol)
            if payload.activo is not None:
                fields.append("activo=%s")
                values.append(1 if payload.activo else 0)
            if payload.password is not None:
                fields.append("password_hash=%s")
                values.append(hash_password(payload.password))

            if fields:
                values.append(row["id"])
                cur.execute(
                    f"UPDATE usuario SET {', '.join(fields)} WHERE id=%s",
                    values,
                )

            cur.execute(
                "SELECT id, username, email, rol, activo FROM usuario WHERE id=%s",
                (row["id"],),
            )
            return _user_to_public(cur, cur.fetchone())


@app.delete("/users/{username}", status_code=204)
def delete_user(username: str, _admin=Depends(require_role("admin"))):
    with get_conn() as conn:
        with conn.cursor() as cur:
            row = _user_by_username(cur, username)
            if not row:
                raise HTTPException(status_code=404, detail="Usuario no encontrado")
            if username == "admin":
                raise HTTPException(
                    status_code=400, detail="No se puede borrar el usuario admin"
                )
            cur.execute("UPDATE usuario SET activo=0 WHERE id=%s", (row["id"],))


# ════════════════════════════════════════════════════════════════════════════
# Cursos
# ════════════════════════════════════════════════════════════════════════════
def _curso_to_public(cur, row: dict) -> CursoPublic:
    profesor_username = None
    if row["profesor_id"]:
        cur.execute(
            "SELECT username FROM usuario WHERE id=%s", (row["profesor_id"],)
        )
        prof_row = cur.fetchone()
        profesor_username = prof_row["username"] if prof_row else None

    cur.execute(
        "SELECT u.username FROM curso_alumno ca "
        "JOIN usuario u ON u.id = ca.alumno_id "
        "WHERE ca.curso_id=%s ORDER BY u.username",
        (row["id"],),
    )
    alumnos = [r["username"] for r in cur.fetchall()]

    cur.execute(
        "SELECT u.username FROM curso_coach cc "
        "JOIN usuario u ON u.id = cc.coach_id "
        "WHERE cc.curso_id=%s ORDER BY u.username",
        (row["id"],),
    )
    coaches = [r["username"] for r in cur.fetchall()]

    return CursoPublic(
        id=row["id"],
        codigo=row["codigo"],
        nombre=row["nombre"],
        profesor_id=row["profesor_id"],
        profesor_username=profesor_username,
        periodo=row["periodo"],
        activo=bool(row["activo"]),
        alumnos=alumnos,
        coaches=coaches,
    )


def _curso_by_codigo(cur, codigo: str) -> Optional[dict]:
    cur.execute(
        "SELECT id, codigo, nombre, profesor_id, periodo, activo "
        "FROM curso WHERE codigo=%s",
        (codigo,),
    )
    return cur.fetchone()


def _check_profesor_owns_curso(user: dict, curso_row: dict):
    """Lanza 403 si el profesor no es dueño del curso. Admin pasa siempre."""
    if user["role"] == "admin":
        return
    if user["role"] == "profesor" and curso_row["profesor_id"] == user["uid"]:
        return
    raise HTTPException(
        status_code=403, detail="Solo el profesor dueño o un admin pueden modificar este curso"
    )


@app.get("/courses", response_model=list[CursoPublic])
def list_courses(user: dict = Depends(current_user)):
    """
    - admin: ve todos los cursos activos
    - profesor: ve los que dicta
    - coach: ve los que audita (asignados vía curso_coach)
    - alumno: ve los que cursa
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            role = user["role"]
            if role == "admin":
                cur.execute(
                    "SELECT id, codigo, nombre, profesor_id, periodo, activo "
                    "FROM curso WHERE activo=1 ORDER BY codigo"
                )
            elif role == "profesor":
                cur.execute(
                    "SELECT id, codigo, nombre, profesor_id, periodo, activo "
                    "FROM curso WHERE profesor_id=%s AND activo=1 ORDER BY codigo",
                    (user["uid"],),
                )
            elif role == "coach":
                cur.execute(
                    "SELECT c.id, c.codigo, c.nombre, c.profesor_id, c.periodo, c.activo "
                    "FROM curso c JOIN curso_coach cc ON cc.curso_id=c.id "
                    "WHERE cc.coach_id=%s AND c.activo=1 ORDER BY c.codigo",
                    (user["uid"],),
                )
            else:  # alumno
                cur.execute(
                    "SELECT DISTINCT c.id, c.codigo, c.nombre, c.profesor_id, c.periodo, c.activo "
                    "FROM curso c JOIN curso_alumno ca ON ca.curso_id=c.id "
                    "WHERE ca.alumno_id=%s AND c.activo=1 ORDER BY c.codigo",
                    (user["uid"],),
                )
            rows = cur.fetchall()
            return [_curso_to_public(cur, r) for r in rows]


@app.post("/courses", response_model=CursoPublic, status_code=201)
def create_course(
    payload: CursoCreateRequest,
    _admin=Depends(require_role("admin")),
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            if _curso_by_codigo(cur, payload.codigo):
                raise HTTPException(status_code=409, detail="El código de curso ya existe")

            profesor_id = None
            if payload.profesor_username:
                prof = _user_by_username(cur, payload.profesor_username)
                if not prof or prof["rol"] != "profesor":
                    raise HTTPException(
                        status_code=400,
                        detail="El usuario indicado no existe o no tiene rol 'profesor'",
                    )
                profesor_id = prof["id"]

            cur.execute(
                "INSERT INTO curso (codigo, nombre, profesor_id, periodo) "
                "VALUES (%s, %s, %s, %s)",
                (payload.codigo, payload.nombre, profesor_id, payload.periodo),
            )
            new_id = cur.lastrowid
            cur.execute(
                "SELECT id, codigo, nombre, profesor_id, periodo, activo "
                "FROM curso WHERE id=%s",
                (new_id,),
            )
            return _curso_to_public(cur, cur.fetchone())


@app.get("/courses/{codigo}", response_model=CursoPublic)
def get_course(codigo: str, user: dict = Depends(current_user)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            row = _curso_by_codigo(cur, codigo)
            if not row:
                raise HTTPException(status_code=404, detail="Curso no encontrado")
            return _curso_to_public(cur, row)


@app.patch("/courses/{codigo}", response_model=CursoPublic)
def update_course(
    codigo: str,
    payload: CursoUpdateRequest,
    user: dict = Depends(current_user),
):
    if user["role"] not in ("admin", "profesor"):
        raise HTTPException(status_code=403, detail="Acción no permitida para tu rol")

    with get_conn() as conn:
        with conn.cursor() as cur:
            row = _curso_by_codigo(cur, codigo)
            if not row:
                raise HTTPException(status_code=404, detail="Curso no encontrado")
            _check_profesor_owns_curso(user, row)

            fields, values = [], []
            if payload.nombre is not None:
                fields.append("nombre=%s")
                values.append(payload.nombre)
            if payload.periodo is not None:
                fields.append("periodo=%s")
                values.append(payload.periodo)
            if payload.activo is not None:
                fields.append("activo=%s")
                values.append(1 if payload.activo else 0)
            if payload.profesor_username is not None:
                # Solo admin puede reasignar profesor
                if user["role"] != "admin":
                    raise HTTPException(
                        status_code=403,
                        detail="Solo un admin puede reasignar el profesor de un curso",
                    )
                prof = _user_by_username(cur, payload.profesor_username)
                if not prof or prof["rol"] != "profesor":
                    raise HTTPException(
                        status_code=400, detail="profesor_username no válido"
                    )
                fields.append("profesor_id=%s")
                values.append(prof["id"])

            if fields:
                values.append(row["id"])
                cur.execute(
                    f"UPDATE curso SET {', '.join(fields)} WHERE id=%s", values
                )

            cur.execute(
                "SELECT id, codigo, nombre, profesor_id, periodo, activo "
                "FROM curso WHERE id=%s",
                (row["id"],),
            )
            return _curso_to_public(cur, cur.fetchone())


@app.delete("/courses/{codigo}", status_code=204)
def delete_course(codigo: str, _admin=Depends(require_role("admin"))):
    with get_conn() as conn:
        with conn.cursor() as cur:
            row = _curso_by_codigo(cur, codigo)
            if not row:
                raise HTTPException(status_code=404, detail="Curso no encontrado")
            cur.execute("UPDATE curso SET activo=0 WHERE id=%s", (row["id"],))


# ─── Inscripciones ──────────────────────────────────────────────────────────
@app.post("/courses/{codigo}/members", response_model=CursoPublic)
def enroll_students(
    codigo: str,
    payload: EnrollmentRequest,
    user: dict = Depends(current_user),
):
    if user["role"] not in ("admin", "profesor"):
        raise HTTPException(status_code=403, detail="Acción no permitida para tu rol")

    with get_conn() as conn:
        with conn.cursor() as cur:
            row = _curso_by_codigo(cur, codigo)
            if not row:
                raise HTTPException(status_code=404, detail="Curso no encontrado")
            _check_profesor_owns_curso(user, row)

            not_found, not_alumno = [], []
            for username in payload.alumno_usernames:
                u = _user_by_username(cur, username)
                if not u:
                    not_found.append(username)
                    continue
                if u["rol"] != "alumno":
                    not_alumno.append(username)
                    continue
                cur.execute(
                    "INSERT IGNORE INTO curso_alumno (curso_id, alumno_id) VALUES (%s, %s)",
                    (row["id"], u["id"]),
                )

            if not_found or not_alumno:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "no_encontrados": not_found,
                        "no_son_alumno": not_alumno,
                    },
                )

            return _curso_to_public(cur, row)


@app.delete("/courses/{codigo}/members/{username}", status_code=204)
def unenroll_student(
    codigo: str,
    username: str,
    user: dict = Depends(current_user),
):
    if user["role"] not in ("admin", "profesor"):
        raise HTTPException(status_code=403, detail="Acción no permitida para tu rol")

    with get_conn() as conn:
        with conn.cursor() as cur:
            row = _curso_by_codigo(cur, codigo)
            if not row:
                raise HTTPException(status_code=404, detail="Curso no encontrado")
            _check_profesor_owns_curso(user, row)

            u = _user_by_username(cur, username)
            if not u:
                raise HTTPException(status_code=404, detail="Alumno no encontrado")

            cur.execute(
                "DELETE FROM curso_alumno WHERE curso_id=%s AND alumno_id=%s",
                (row["id"], u["id"]),
            )

# ════════════════════════════════════════════════════════════════════════════
# Asignación de coaches a cursos (M:N)
# ════════════════════════════════════════════════════════════════════════════
@app.post("/courses/{codigo}/coaches", response_model=CursoPublic)
def assign_coaches(
    codigo: str,
    payload: CoachAssignmentRequest,
    _admin=Depends(require_role("admin")),
):
    """Asigna uno o más coaches a un curso. Solo admin."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            row = _curso_by_codigo(cur, codigo)
            if not row:
                raise HTTPException(status_code=404, detail="Curso no encontrado")

            not_found, not_coach = [], []
            for username in payload.coach_usernames:
                u = _user_by_username(cur, username)
                if not u:
                    not_found.append(username)
                    continue
                if u["rol"] != "coach":
                    not_coach.append(username)
                    continue
                cur.execute(
                    "INSERT IGNORE INTO curso_coach (curso_id, coach_id) VALUES (%s, %s)",
                    (row["id"], u["id"]),
                )

            if not_found or not_coach:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "no_encontrados": not_found,
                        "no_son_coach": not_coach,
                    },
                )

            return _curso_to_public(cur, row)


@app.delete("/courses/{codigo}/coaches/{username}", status_code=204)
def remove_coach(
    codigo: str,
    username: str,
    _admin=Depends(require_role("admin")),
):
    """Quita un coach de un curso. Solo admin."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            row = _curso_by_codigo(cur, codigo)
            if not row:
                raise HTTPException(status_code=404, detail="Curso no encontrado")

            u = _user_by_username(cur, username)
            if not u:
                raise HTTPException(status_code=404, detail="Coach no encontrado")

            cur.execute(
                "DELETE FROM curso_coach WHERE curso_id=%s AND coach_id=%s",
                (row["id"], u["id"]),
            )


# ════════════════════════════════════════════════════════════════════════════
# Listados públicos para flujos de UI (no requieren ser admin)
# ════════════════════════════════════════════════════════════════════════════
@app.get("/students-listable", response_model=list[UserPublic])
def list_students_for_enrollment(user: dict = Depends(current_user)):
    """
    Lista de alumnos disponibles para flujos como "inscribir alumno en curso"
    o "crear slice on behalf of alumno". Visible para admin y profesor.

    Existe porque GET /users es admin-only y bloqueaba al profesor.
    """
    if user["role"] not in ("admin", "profesor"):
        raise HTTPException(status_code=403, detail="Acción no permitida para tu rol")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, email, rol, activo "
                "FROM usuario WHERE rol='alumno' AND activo=1 ORDER BY username"
            )
            rows = cur.fetchall()
            return [_user_to_public(cur, r) for r in rows]


@app.get("/coaches-listable", response_model=list[UserPublic])
def list_coaches_for_assignment(_admin=Depends(require_role("admin"))):
    """Lista de coaches disponibles para asignar a un curso. Solo admin."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, email, rol, activo "
                "FROM usuario WHERE rol='coach' AND activo=1 ORDER BY username"
            )
            rows = cur.fetchall()
            return [_user_to_public(cur, r) for r in rows]
