#!/usr/bin/env bash
# ============================================================================
# preflight.sh — Chequeo previo a la demo (Grupo 6 · TEL141)
# READ-ONLY: solo diagnostica, NO borra ni modifica nada.
# Cubre: stack local (VM app) + cluster Linux + cluster OpenStack.
#
# Uso (en la VM app, con los túneles ya levantados):
#   ./preflight.sh            # chequeo completo
#   ./preflight.sh --hints    # además imprime comandos de recuperación
#   SKIP_OS=1 ./preflight.sh  # omite el cluster OpenStack
#
# Sale con código 0 si no hay FAIL (WARN no bloquea).
# ============================================================================

# ── Config (sobre-escribible por env) ──────────────────────────────────────
UI_BASE="${UI_BASE:-http://localhost:8500}"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-admin123}"

GW="${GW:-10.20.11.189}"
SSH_USER="${SSH_USER:-ubuntu}"
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=6 -o StrictHostKeyChecking=accept-new"

# Puertos SSH (vía gateway) de cada worker
declare -A LINUX_PORTS=( [server1]=5811 [server2]=5812 [server3]=5813 [headnode]=5814 )
declare -A OS_PORTS=( [os-headnode]=5821 [worker1]=5822 [worker2]=5823 [worker3]=5824 )

# Túneles locales de monitoreo esperados
LINUX_TUN_PORTS=(9101 9102 9103)
OS_TUN_PORTS=(9122 9123 9124)

PROM="${PROM:-http://localhost:9090}"
OS_OPENRC="${OS_OPENRC:-~/env-scripts/cloud-admin-openrc}"

# Contenedores esperados con nombre fijo (redis + rq_worker se detectan aparte)
NAMED_CONTAINERS=(pucp_mariadb pucp_auth pucp_placement pucp_reconcile \
                  pucp_image pucp_slice pucp_gateway pucp_prometheus \
                  pucp_grafana pucp_alertmanager)

# ── Colores / contadores ───────────────────────────────────────────────────
if [ -t 1 ]; then G="\033[32m"; Y="\033[33m"; R="\033[31m"; B="\033[1m"; N="\033[0m"; else G=""; Y=""; R=""; B=""; N=""; fi
FAILS=0; WARNS=0
ok()   { echo -e "  ${G}[OK]${N}   $*"; }
warn() { echo -e "  ${Y}[WARN]${N} $*"; WARNS=$((WARNS+1)); }
fail() { echo -e "  ${R}[FAIL]${N} $*"; FAILS=$((FAILS+1)); }
head() { echo -e "\n${B}== $* ==${N}"; }

ssh_w() { # ssh_w <port> <cmd...>
  local port="$1"; shift
  ssh $SSH_OPTS -p "$port" "${SSH_USER}@${GW}" "$@" 2>/dev/null
}

pyjson() { python3 -c "import sys,json;$1" 2>/dev/null; }

# ── A. Stack local (común a ambos clusters) ────────────────────────────────
head "A. Stack local (VM app)"

RUNNING="$(sudo docker ps --format '{{.Names}}' 2>/dev/null)"
if [ -z "$RUNNING" ]; then
  fail "docker no responde o no hay contenedores corriendo"
else
  missing=()
  for c in "${NAMED_CONTAINERS[@]}"; do
    echo "$RUNNING" | grep -qx "$c" || missing+=("$c")
  done
  # redis + rq_worker no tienen container_name fijo
  echo "$RUNNING" | grep -qiE 'redis' || missing+=("redis")
  sudo docker ps --format '{{.Command}}' 2>/dev/null | grep -qi 'rq' || missing+=("rq_worker")
  if [ ${#missing[@]} -eq 0 ]; then
    ok "12/12 contenedores arriba"
  else
    fail "contenedores caídos: ${missing[*]}  → sudo docker-compose up -d"
  fi
fi

# Gateway + token admin (una llamada valida ambos)
TOK="$(curl -s -m 8 -X POST "$UI_BASE/auth/login" \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}" \
        | pyjson "print(json.load(sys.stdin).get('access_token',''))")"
if [ -n "$TOK" ]; then
  ok "Gateway 8500 responde y token admin generado"
else
  fail "no se pudo obtener token admin (¿gateway/auth caído?)  → revisar pucp_gateway/pucp_auth"
fi

# Redis: colas
RCON="$(sudo docker ps --format '{{.Names}}' | grep -iE 'redis' | head -1)"
if [ -n "$RCON" ]; then
  QLEN="$(sudo docker exec "$RCON" redis-cli -n 0 LLEN 'rq:queue:slices' 2>/dev/null | tr -d '\r')"
  QFAIL="$(sudo docker exec "$RCON" redis-cli -n 0 LLEN 'rq:queue:failed' 2>/dev/null | tr -d '\r')"
  [ "${QLEN:-0}" = "0" ] && ok "cola 'slices' vacía" || warn "cola 'slices' con ${QLEN} jobs pendientes (¿deploy colgado?)"
  [ "${QFAIL:-0}" = "0" ] && ok "sin jobs en 'failed'" || warn "${QFAIL} jobs en cola 'failed' (residuo de ensayos)"
else
  warn "no encontré el contenedor de redis para revisar colas"
fi

# MariaDB: recursos ya comprometidos (residuo de ensayo previo)
DBOUT="$(sudo docker exec pucp_mariadb mariadb -u pucp -ppucp_pass pucp_cloud -N -B \
  -e "SELECT nombre,vcpus_used,ram_used_mb,storage_used_gb FROM servidor_fisico;" 2>/dev/null)"
if [ -n "$DBOUT" ]; then
  BUSY="$(echo "$DBOUT" | awk '$2+0>0 || $3+0>0 || $4+0>0 {print $1"(cpu="$2",ram="$3"MB,disk="$4"GB)"}' | tr '\n' ' ')"
  [ -z "$BUSY" ] && ok "MariaDB: ningún worker con recursos reservados" \
                 || warn "MariaDB con reservas activas: $BUSY  (¿slices vivos o drift? revisar reconcile)"
else
  warn "no pude leer servidor_fisico en MariaDB"
fi

# ── B. Túneles + Prometheus (ambos clusters) ───────────────────────────────
head "B. Túneles y monitoreo"

check_tun() { # check_tun <label> port...
  local label="$1"; shift; local down=()
  for p in "$@"; do
    (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null && exec 3>&- || down+=("$p")
  done
  [ ${#down[@]} -eq 0 ] && ok "túneles $label OK (${*})" \
                        || fail "túneles $label caídos: ${down[*]}  → correr ~/start_tunnels.sh"
}
check_tun "Linux" "${LINUX_TUN_PORTS[@]}"
[ "${SKIP_OS:-0}" = "1" ] || check_tun "OpenStack" "${OS_TUN_PORTS[@]}"

UPCNT="$(curl -s -m 8 "$PROM/api/v1/query?query=up" \
  | pyjson "d=json.load(sys.stdin);print(sum(1 for r in d.get('data',{}).get('result',[]) if r['value'][1]=='1'))")"
if [ -n "$UPCNT" ]; then
  [ "${UPCNT:-0}" -ge 6 ] && ok "Prometheus: ${UPCNT} node_exporter arriba (esperado 6)" \
                          || warn "Prometheus: solo ${UPCNT}/6 node_exporter arriba (túnel o worker caído)"
else
  warn "Prometheus 9090 no respondió la query 'up'"
fi

# ── C. Cluster Linux ───────────────────────────────────────────────────────
head "C. Cluster Linux (workers vía gateway)"

for name in server1 server2 server3; do
  port="${LINUX_PORTS[$name]}"
  if ! ssh_w "$port" "echo ok" | grep -q ok; then
    fail "$name (p$port): SSH no responde"
    continue
  fi
  drop="$(ssh_w "$port" "sudo ovs-ofctl dump-flows br-int 2>/dev/null | grep -c actions=drop")"
  qemu="$(ssh_w "$port" "ps aux | grep -c '[q]emu-system'")"
  dns="$(ssh_w "$port" "ps aux | grep -c '[d]nsmasq'")"
  drop="${drop:-0}"; qemu="${qemu:-0}"; dns="${dns:-0}"
  line="$name (p$port): qemu=$qemu dnsmasq=$dns"
  if [ "$drop" -gt 0 ]; then
    fail "$line  ${R}drop=$drop ⚠ regla drop huérfana → rompe L2 cross-worker${N}"
  else
    ok "$line  sin regla drop"
  fi
done

# server4 / headnode: NUNCA debe alojar VMs
hport="${LINUX_PORTS[headnode]}"
if ssh_w "$hport" "echo ok" | grep -q ok; then
  hq="$(ssh_w "$hport" "ps aux | grep -c '[q]emu-system'")"; hq="${hq:-0}"
  [ "$hq" = "0" ] && ok "headnode/server4 (p$hport): sin VMs (correcto)" \
                  || warn "headnode/server4 tiene $hq QEMU (el placement NO debe poner VMs aquí)"
else
  warn "headnode/server4 (p$hport): SSH no responde"
fi

# ── D. Cluster OpenStack ───────────────────────────────────────────────────
if [ "${SKIP_OS:-0}" != "1" ]; then
head "D. Cluster OpenStack"

hp="${OS_PORTS[os-headnode]}"
if ssh_w "$hp" "echo ok" | grep -q ok; then
  ok "os-headnode (p$hp): SSH responde"

  # Servicios de cómputo Nova arriba
  svc="$(ssh_w "$hp" ". $OS_OPENRC && openstack compute service list -f value -c Binary -c State 2>/dev/null")"
  if [ -n "$svc" ]; then
    down="$(echo "$svc" | awk '$2!="up"{print $1}' | tr '\n' ' ')"
    [ -z "$down" ] && ok "Nova: todos los servicios 'up'" \
                   || fail "Nova con servicios caídos: $down"
  else
    warn "no pude listar 'compute service list' (¿openrc o keystone?)"
  fi

  # Agentes de red Neutron
  nag="$(ssh_w "$hp" ". $OS_OPENRC && openstack network agent list -f value -c Alive 2>/dev/null")"
  if [ -n "$nag" ]; then
    ndead="$(echo "$nag" | grep -vic 'true\|:-)')"
    [ "${ndead:-0}" = "0" ] && ok "Neutron: agentes vivos" \
                            || warn "Neutron: ${ndead} agente(s) no vivos"
  fi

  # VMs y redes huérfanas (residuo de ensayos → causa 409 y consumo)
  vms="$(ssh_w "$hp" ". $OS_OPENRC && openstack server list --all-projects -f value -c Name 2>/dev/null")"
  vcount="$(echo -n "$vms" | grep -c . )"
  [ "${vcount:-0}" = "0" ] && ok "sin instancias OpenStack activas" \
       || warn "${vcount} instancia(s) OpenStack existentes: $(echo "$vms" | tr '\n' ' ')  (¿residuo?)"

  nets="$(ssh_w "$hp" ". $OS_OPENRC && openstack network list -f value -c Name 2>/dev/null | grep -iE 'slice|net-|pucp' ")"
  ncount="$(echo -n "$nets" | grep -c . )"
  [ "${ncount:-0}" = "0" ] && ok "sin redes de slice huérfanas" \
       || warn "${ncount} red(es) de slice existentes: $(echo "$nets" | tr '\n' ' ')  (pueden ocupar VLANs → 409)"
else
  fail "os-headnode (p$hp): SSH no responde  → sin esto no valida OpenStack"
fi

# Compute nodes: QEMU (instancias) + SSH
for name in worker1 worker2 worker3; do
  port="${OS_PORTS[$name]}"
  if ssh_w "$port" "echo ok" | grep -q ok; then
    q="$(ssh_w "$port" "ps aux | grep -c '[q]emu-system'")"; q="${q:-0}"
    ok "$name (p$port): SSH ok, qemu=$q"
  else
    warn "$name (p$port): SSH no responde (compute node)"
  fi
done
else
  head "D. Cluster OpenStack"
  echo "  (omitido por SKIP_OS=1)"
fi

# ── E. Estado de slices (ambos) ────────────────────────────────────────────
head "E. Estado de slices en el orquestador"
if [ -n "$TOK" ]; then
  SL="$(curl -s -m 8 "$UI_BASE/api/graph-slices" -H "Authorization: Bearer $TOK")"
  SUMMARY="$(echo "$SL" | pyjson "
d=json.load(sys.stdin)
from collections import Counter
c=Counter(s.get('state','?') for s in d) if isinstance(d,list) else {}
print('|'.join(f'{k}={v}' for k,v in c.items()) or 'ninguno')
bad=[s['slice_name'] for s in d if isinstance(d,list) and s.get('state') in ('failed','deleting','queued')]
print(','.join(bad))")"
  states="$(echo "$SUMMARY" | sed -n 1p)"
  stuck="$(echo "$SUMMARY" | sed -n 2p)"
  ok "slices: ${states}"
  [ -n "$stuck" ] && warn "slices en estado transitorio/fallido: $stuck  (limpiar antes de la demo)"
else
  warn "sin token: no pude listar slices"
fi

# ── Resumen ────────────────────────────────────────────────────────────────
echo
if [ "$FAILS" -eq 0 ] && [ "$WARNS" -eq 0 ]; then
  echo -e "${G}${B}LISTO PARA DEMO ✅  (0 fallos, 0 warnings)${N}"
elif [ "$FAILS" -eq 0 ]; then
  echo -e "${Y}${B}CASI LISTO ⚠  ($WARNS warning(s), 0 fallos) — revisa los WARN pero no bloquean${N}"
else
  echo -e "${R}${B}NO LISTO ❌  ($FAILS fallo(s), $WARNS warning(s)) — resuelve los FAIL antes de empezar${N}"
fi

# ── Hints de recuperación (solo imprime, no ejecuta) ───────────────────────
if [ "${1:-}" = "--hints" ] || [ "${HINTS:-0}" = "1" ]; then
  cat <<'HINTS'

── Comandos de recuperación (copiar/pegar según el problema) ───────────────
Token vencido/vacío:
  TOK_ADMIN=$(curl -s -X POST http://localhost:8500/auth/login \
    -H "Content-Type: application/json" \
    -d '{"username":"admin","password":"admin123"}' | jq -r .access_token)

Túneles caídos:
  ~/start_tunnels.sh

Contenedor(es) caído(s):
  cd ~/TEL141_CLOUD_G6 && sudo docker-compose up -d
  # tras cambios en slice_manager:  sudo docker-compose restart slice_manager rq_worker

Regla DROP huérfana en un worker Linux (rompe L2 cross-worker):
  ssh ubuntu@10.20.11.189 -p 58XX "sudo ovs-ofctl del-flows br-int 'vlan_tci=0x0000'"

Instancias/redes huérfanas en OpenStack (causan 409 y ocupan VLANs):
  ssh ubuntu@10.20.11.189 -p 5821 \
    ". ~/env-scripts/cloud-admin-openrc && openstack server list --all-projects"
  # borrar lo que sea residuo:  openstack server delete <id> ;  openstack network delete <id>

Reset limpio completo del lab (¡destructivo, solo si hace falta!):
  bash scripts/cleanup_all_lab.sh
HINTS
fi

exit $([ "$FAILS" -eq 0 ] && echo 0 || echo 1)
