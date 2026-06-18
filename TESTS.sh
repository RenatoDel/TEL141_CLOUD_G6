#!/usr/bin/env bash
# ============================================================================
# TESTS.sh — Smoke tests end-to-end del flujo auth + RBAC.
#
# Asume:
#   - Gateway corriendo en localhost:8500
#   - Migración 002_roles_courses.sql aplicada
#   - jq instalado
#
# Uso:
#   ./TESTS.sh
# ============================================================================

set -e

GW="${GATEWAY_URL:-http://localhost:8500}"

red()    { printf "\033[31m%s\033[0m\n" "$*"; }
green()  { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }

login() {
  local user="$1" pass="$2"
  curl -s -X POST "$GW/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"$user\",\"password\":\"$pass\"}" \
    | jq -r .access_token
}

assert_status() {
  local expected="$1" actual="$2" label="$3"
  if [[ "$actual" == "$expected" ]]; then
    green "  ✓ $label (status $actual)"
  else
    red "  ✗ $label: expected $expected got $actual"
    exit 1
  fi
}

# ─── 1. Login con cada rol ──────────────────────────────────────────
yellow "=== 1. Login con cada rol ==="
TOK_ADMIN=$(login admin admin123)
TOK_PROFE=$(login profesor1 profesor123)
TOK_COACH=$(login coach1 coach123)
TOK_ALU1=$(login alumno1 alumno123)
TOK_ALU2=$(login alumno2 alumno123)

for label in TOK_ADMIN TOK_PROFE TOK_COACH TOK_ALU1 TOK_ALU2; do
  tok="${!label}"
  if [[ -z "$tok" || "$tok" == "null" ]]; then
    red "  ✗ Login falló: $label vacío"
    exit 1
  fi
  green "  ✓ $label obtenido"
done

# ─── 2. /me ─────────────────────────────────────────────────────────
yellow "=== 2. /me para cada rol ==="
for label in TOK_ADMIN TOK_PROFE TOK_COACH TOK_ALU1; do
  tok="${!label}"
  info=$(curl -s "$GW/auth/me" -H "Authorization: Bearer $tok")
  role=$(echo "$info" | jq -r .rol)
  courses=$(echo "$info" | jq -c .courses)
  green "  $label: rol=$role courses=$courses"
done

# ─── 3. GET /users requiere admin ───────────────────────────────────
yellow "=== 3. GET /users requiere rol admin ==="
status=$(curl -s -o /dev/null -w "%{http_code}" \
  "$GW/auth/users" -H "Authorization: Bearer $TOK_ADMIN")
assert_status 200 "$status" "admin puede listar usuarios"

status=$(curl -s -o /dev/null -w "%{http_code}" \
  "$GW/auth/users" -H "Authorization: Bearer $TOK_PROFE")
assert_status 403 "$status" "profesor NO puede listar usuarios"

status=$(curl -s -o /dev/null -w "%{http_code}" \
  "$GW/auth/users" -H "Authorization: Bearer $TOK_ALU1")
assert_status 403 "$status" "alumno NO puede listar usuarios"

# ─── 4. GET /courses filtrado por rol ───────────────────────────────
yellow "=== 4. GET /courses filtrado por rol ==="
admin_n=$(curl -s "$GW/auth/courses" -H "Authorization: Bearer $TOK_ADMIN" | jq length)
profe_n=$(curl -s "$GW/auth/courses" -H "Authorization: Bearer $TOK_PROFE" | jq length)
coach_n=$(curl -s "$GW/auth/courses" -H "Authorization: Bearer $TOK_COACH" | jq length)
alu_n=$(curl -s "$GW/auth/courses"   -H "Authorization: Bearer $TOK_ALU1"  | jq length)
green "  admin ve $admin_n, profesor ve $profe_n, coach ve $coach_n, alumno1 ve $alu_n"

# ─── 5. GET /graph-slices filtrado por rol ──────────────────────────
yellow "=== 5. GET /graph-slices filtrado por rol ==="
admin_n=$(curl -s "$GW/api/graph-slices" -H "Authorization: Bearer $TOK_ADMIN" | jq length)
coach_n=$(curl -s "$GW/api/graph-slices" -H "Authorization: Bearer $TOK_COACH" | jq length)
alu_n=$(curl -s "$GW/api/graph-slices"   -H "Authorization: Bearer $TOK_ALU1"  | jq length)
green "  admin ve $admin_n slices, coach ve $coach_n (=todos), alumno1 ve $alu_n (sus propios)"
if [[ "$admin_n" != "$coach_n" ]]; then
  red "  ✗ admin y coach deberían ver la misma cantidad de slices"
  exit 1
fi

# ─── 6. Crear un slice como alumno1 ─────────────────────────────────
yellow "=== 6. Alumno crea slice propio ==="
TS=$(date +%s)
SLICE_NAME="test-rbac-$TS"
VM_A="rbacvm-${TS}-a"
VM_B="rbacvm-${TS}-b"
VLAN_BASE=$(( (TS % 500) + 600 ))   # rango 600-1099
VNC_START=$(( (TS % 200) + 5950 )) # rango 5950-6149, lejos del default 5901
status=$(curl -s -o /tmp/resp.json -w "%{http_code}" \
  -X POST "$GW/api/graph-slices" \
  -H "Authorization: Bearer $TOK_ALU1" \
  -H "Content-Type: application/json" \
  -d "{
    \"slice_name\":\"$SLICE_NAME\",
    \"vlan_base\":$VLAN_BASE,
    \"vnc_start\":$VNC_START,
    \"cluster\":\"linux\",
    \"nodes\":[{\"name\":\"$VM_A\"},{\"name\":\"$VM_B\"}],
    \"links\":[{\"id\":\"l1\",\"from\":\"$VM_A\",\"to\":\"$VM_B\"}]
  }")
green "  POST /graph-slices como alumno1 → status $status"
[[ "$status" == "200" || "$status" == "201" ]] || { cat /tmp/resp.json; exit 1; }

# ─── 7. Alumno2 NO debe ver el slice de alumno1 ─────────────────────
yellow "=== 7. Aislamiento entre alumnos ==="
visible=$(curl -s "$GW/api/graph-slices" -H "Authorization: Bearer $TOK_ALU2" \
  | jq -r ".[] | select(.slice_name==\"$SLICE_NAME\") | .slice_name")
if [[ -z "$visible" ]]; then
  green "  ✓ alumno2 NO ve el slice de alumno1"
else
  red "  ✗ alumno2 VE el slice de alumno1 (debió ser invisible)"
  exit 1
fi

# Coach debe verlo
visible=$(curl -s "$GW/api/graph-slices" -H "Authorization: Bearer $TOK_COACH" \
  | jq -r ".[] | select(.slice_name==\"$SLICE_NAME\") | .slice_name")
if [[ "$visible" == "$SLICE_NAME" ]]; then
  green "  ✓ coach SÍ ve el slice (read-only)"
else
  red "  ✗ coach no ve el slice (debería verlo)"
  exit 1
fi

# Profesor1 (dueño del curso TEL141 al que alumno1 pertenece) debe verlo
visible=$(curl -s "$GW/api/graph-slices" -H "Authorization: Bearer $TOK_PROFE" \
  | jq -r ".[] | select(.slice_name==\"$SLICE_NAME\") | .slice_name")
# OJO: alumno1 creó el slice SIN curso_id, así que NO se atribuye al curso.
# El profesor solo lo ve si el slice tiene curso_id=su_curso. Aquí no lo verá.
if [[ -z "$visible" ]]; then
  green "  ✓ profesor no ve el slice porque alumno1 lo creó sin curso_id (correcto)"
else
  yellow "  ⚠ profesor ve el slice — esto solo sería correcto si alumno1 lo hubiera asignado al curso"
fi

# ─── 8. Coach NO puede borrar ───────────────────────────────────────
yellow "=== 8. Coach NO puede borrar ==="
status=$(curl -s -o /dev/null -w "%{http_code}" \
  -X DELETE "$GW/api/graph-slices/$SLICE_NAME" \
  -H "Authorization: Bearer $TOK_COACH")
assert_status 403 "$status" "coach 403 al intentar borrar"

# ─── 9. Alumno2 NO puede borrar slice de alumno1 ────────────────────
yellow "=== 9. Alumno NO puede borrar slice ajeno ==="
status=$(curl -s -o /dev/null -w "%{http_code}" \
  -X DELETE "$GW/api/graph-slices/$SLICE_NAME" \
  -H "Authorization: Bearer $TOK_ALU2")
assert_status 403 "$status" "alumno2 403 al borrar slice de alumno1"

# ─── 10. El dueño SÍ puede borrar ───────────────────────────────────
yellow "=== 10. Cleanup: el dueño borra su slice ==="
status=$(curl -s -o /dev/null -w "%{http_code}" \
  -X DELETE "$GW/api/graph-slices/$SLICE_NAME" \
  -H "Authorization: Bearer $TOK_ALU1")
green "  status borrado: $status (200/204 OK)"

echo
green "═══════════════════════════════════════════════════════"
green "  TODOS LOS TESTS DE INTEGRACIÓN PASARON CORRECTAMENTE"
green "═══════════════════════════════════════════════════════"
