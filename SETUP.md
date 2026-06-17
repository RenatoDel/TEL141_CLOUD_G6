# PUCP Auth & RBAC — Guía de despliegue

Este paquete agrega autenticación basada en DB y autorización por roles
(`admin`, `profesor`, `coach`, `alumno`) al PUCP Cloud Orchestrator.

## 1. Resumen de cambios

```
sql/migrations/002_roles_courses.sql      ← NUEVO (migración idempotente)

auth_service/Dockerfile                   ← MODIFICADO (deps: bcrypt, pymysql)
auth_service/app/db.py                    ← NUEVO
auth_service/app/security.py              ← NUEVO
auth_service/app/models.py                ← NUEVO
auth_service/app/main.py                  ← REESCRITO

slice_manager/app/auth.py                 ← MODIFICADO (defaults defensivos)
slice_manager/app/rbac.py                 ← NUEVO
slice_manager/app/schemas.py              ← MODIFICADO (owner_username, curso_id)
slice_manager/app/graph_schemas.py        ← MODIFICADO (owner_username, curso_id)
slice_manager/app/main.py                 ← REESCRITO (RBAC en cada endpoint)
```

No se tocaron: `gateway`, `placement_service`, `image_service`,
`linux_backend/driver.py`, `openstack_backend/driver.py`. El JWT viaja
transparente vía la cabecera `Authorization` que el gateway ya forwarda.

## 2. Pasos de aplicación

### 2.1 Copiar los archivos al repo

```bash
cd ~/TEL141_CLOUD_G6

# Migración SQL (idempotente, se puede aplicar sobre la DB existente)
mkdir -p sql/migrations
cp ruta/al/paquete/sql/migrations/002_roles_courses.sql sql/migrations/

# Auth service (reescritura completa)
cp ruta/al/paquete/auth_service/Dockerfile         auth_service/Dockerfile
cp ruta/al/paquete/auth_service/app/db.py          auth_service/app/db.py
cp ruta/al/paquete/auth_service/app/security.py    auth_service/app/security.py
cp ruta/al/paquete/auth_service/app/models.py      auth_service/app/models.py
cp ruta/al/paquete/auth_service/app/main.py        auth_service/app/main.py

# Slice manager (parches)
cp ruta/al/paquete/slice_manager/app/auth.py          slice_manager/app/auth.py
cp ruta/al/paquete/slice_manager/app/rbac.py          slice_manager/app/rbac.py
cp ruta/al/paquete/slice_manager/app/schemas.py       slice_manager/app/schemas.py
cp ruta/al/paquete/slice_manager/app/graph_schemas.py slice_manager/app/graph_schemas.py
cp ruta/al/paquete/slice_manager/app/main.py          slice_manager/app/main.py
```

### 2.2 Aplicar migración SQL sobre la DB existente

```bash
sudo docker exec -i pucp_mariadb mariadb -u pucp -ppucp_pass pucp_cloud \
  < sql/migrations/002_roles_courses.sql
```

Esto:
- Extiende el ENUM `usuario.rol` para incluir `profesor`, `coach`, `alumno`.
- Crea tablas `curso` y `curso_alumno`.
- Da al `admin` su password real (`admin123`) si tenía el placeholder.
- Crea usuarios y curso de demo:
  - `admin` / `admin123` — rol admin
  - `profesor1` / `profesor123` — dicta curso TEL141
  - `coach1` / `coach123` — read-only global
  - `alumno1`, `alumno2` / `alumno123` — inscritos en TEL141

### 2.3 Backfill de slices existentes (opcional pero recomendado)

Los slices ya creados antes de este cambio no tienen `owner_username` en
`slices.json`. Sin esto, los alumnos no los verán. Si el deployment actual
todo está bajo el admin, asigna ownership = admin:

```bash
sudo docker exec pucp_slice python3 -c "
import json
from pathlib import Path
p = Path('/app/state/slices.json')
if p.exists():
    data = json.loads(p.read_text() or '[]')
    for s in data:
        s.setdefault('owner_username', 'admin')
        s.setdefault('owner_uid', 1)
        s.setdefault('curso_id', None)
        s.setdefault('created_by', 'admin')
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f'Actualizados {len(data)} slices')
"
```

### 2.4 Rebuild de los servicios

```bash
sudo docker-compose up -d --build --no-deps auth_service slice_manager
```

`slice_manager` usa bind-mount (ya configurado en `docker-compose.yml`),
así que estrictamente sólo necesita `restart`. Pero `auth_service` usa
`COPY` y nuevas dependencias (bcrypt, pymysql), entonces requiere `--build`.

### 2.5 Verificar

```bash
# Health checks
curl -s http://localhost:8500/auth/health
curl -s http://localhost:8500/api/health

# Login admin
curl -s -X POST http://localhost:8500/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}' | jq .
```

## 3. Modelo de roles aplicado

| Acción                              | admin | profesor      | coach | alumno  |
|-------------------------------------|:-----:|:-------------:|:-----:|:-------:|
| Ver todos los slices                |  ✓   |  sus cursos  |   ✓  | propios |
| Crear slice propio                  |  ✓   |      ✓       |   ✗  |   ✓     |
| Crear slice para otro alumno        |  ✓   |  sus cursos  |   ✗  |   ✗     |
| Editar / borrar slice               |  ✓   |  sus cursos  |   ✗  | propios |
| Acciones VM (start/stop/reboot)     |  ✓   |  sus cursos  |   ✗  | propias |
| Gestionar usuarios                  |  ✓   |      ✗       |   ✗  |   ✗     |
| Crear/borrar cursos                 |  ✓   |      ✗       |   ✗  |   ✗     |
| Editar curso propio                 |  ✓   |  sus cursos  |   ✗  |   ✗     |
| Inscribir / desinscribir alumnos    |  ✓   |  sus cursos  |   ✗  |   ✗     |
| Ver monitoreo del sistema           |  ✓   |      ✓       |   ✓  |   ✓     |

El **coach** es estrictamente read-only sobre slices y cursos —
diseñado para que un coach/jurado pueda auditar el sistema sin riesgo
de modificación accidental.

## 4. Estructura del JWT emitido

```json
{
  "sub": "alumno1",
  "uid": 5,
  "role": "alumno",
  "email": "alumno1@pucp.edu.pe",
  "courses": [1],
  "exp": 1718000000
}
```

`courses` se llena en el momento del login:
- alumno → cursos en los que está inscrito
- profesor → cursos que dicta
- admin/coach → `[]` (no se filtra por curso)

**Importante:** los cursos no se refrescan automáticamente. Si un alumno
es inscrito en un curso nuevo, debe hacer logout/login para que su token
incluya el nuevo `course_id`. El TTL por defecto es 12 horas
(`JWT_TTL_HOURS`).

## 5. Backwards compatibility

- Tokens emitidos por la versión vieja del `auth_service` (que sólo
  tenían `sub` y `role=admin`) siguen siendo válidos. El `auth.py` del
  slice_manager les añade defaults defensivos: rol `admin`, courses `[]`.
- El endpoint `POST /login` mantiene la misma firma de request y response,
  sólo que ahora `user.role` puede ser cualquiera de los 4 roles.

## 6. Variables de entorno nuevas

| Variable          | Default                         | Propósito                |
|-------------------|---------------------------------|--------------------------|
| `JWT_TTL_HOURS`   | `12`                            | Vida del token en horas  |
| `DB_HOST`         | `mariadb`                       | Host MariaDB             |
| `DB_USER`         | `pucp`                          | Usuario DB               |
| `DB_PASS`         | `pucp_pass`                     | Password DB              |
| `DB_NAME`         | `pucp_cloud`                    | Schema DB                |

`auth_service` ya recibía `DB_HOST/USER/PASS/NAME` en el `docker-compose.yml`
existente; ahora finalmente los usa.

## 7. Cheatsheet de pruebas curl

Ver `TESTS.sh` (incluido en este paquete).

## 8. Roadmap sugerido

- Si quieres migrar el estado de slices de `slices.json` a tabla `slice` en
  MariaDB, está pendiente como mejora. Tendrías índices, joins con curso,
  y resolverías de paso el bug del campo `cluster` que documentaste en R5.
- La UI nueva (siguiente paso) consume:
  - `POST /auth/login` para obtener token.
  - `GET /auth/me` para conocer rol y cursos del usuario.
  - `GET /auth/users`, `GET /auth/courses` para vistas admin/profesor.
  - `GET /api/graph-slices` (filtrado por rol automáticamente).
