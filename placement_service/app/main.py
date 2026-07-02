"""

Reemplaza el round-robin en memoria por un solver CP-SAT (OR-Tools).
Modelo: Multidimensional Knapsack con balanceo de carga.
  - Restricciones duras: CPU, RAM, disco por worker
  - Objetivo: minimizar M_max - M_min (balance de utilización)
  - Over-commitment dinámico con datos de Prometheus + MariaDB
  - Warm start heurístico (Greedy Least Loaded) como hint para CP-SAT
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx
import pymysql
import pymysql.cursors
from fastapi import FastAPI, HTTPException
from ortools.sat.python import cp_model
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("placement_service")

app = FastAPI(title="PUCP Placement Service v2 — CP-SAT", version="2.0.0")

# Over-commitment máximo permitido (configurable por .env)
MAX_OC_CPU: float = float(os.getenv("MAX_OC_CPU", "3.0"))
MAX_OC_RAM: float = float(os.getenv("MAX_OC_RAM", "1.2"))
OC_FALLBACK: float = float(os.getenv("OC_FALLBACK", "1.0"))
MAX_TIME_SOLVER: float = float(os.getenv("MAX_TIME_SOLVER", "0.5"))
PROMETHEUS_WINDOW: str = os.getenv("PROMETHEUS_WINDOW", "10m")
PROMETHEUS_URL: str = os.getenv("PROMETHEUS_URL", "http://10.0.10.4:9090")
RISK_FACTOR_K: float = float(os.getenv("RISK_FACTOR_K", "1.0"))

# MariaDB
DB_HOST: str = os.getenv("DB_HOST", "mariadb")
DB_PORT: int = int(os.getenv("DB_PORT", "3306"))
DB_USER: str = os.getenv("DB_USER", "pucp")
DB_PASS: str = os.getenv("DB_PASS", "pucp_pass")
DB_NAME: str = os.getenv("DB_NAME", "pucp_cloud")

# Escala entera para CP-SAT (no admite decimales)
SCALE = 1000


# ---------------------------------------------------------------------------
# Modelos Pydantic
# ---------------------------------------------------------------------------

class VMSpec(BaseModel):
    """Especificación de recursos de una VM individual."""
    vm_id: str
    cpu: int = Field(ge=1, description="vCPUs solicitados")
    ram_gb: float = Field(gt=0, description="GB de RAM solicitados")
    disk_gb: float = Field(gt=0, description="GB de disco solicitados")


class PlacementRequest(BaseModel):
    """
    Request al endpoint POST /place.
    Reemplaza el viejo {vm_count, availability_zone} por specs reales por VM.
    """
    vms: list[VMSpec] = Field(min_length=1)
    zone: Optional[str] = None
    cluster: str = Field(default="linux")


class PlacementResponse(BaseModel):
    success: bool
    assignments: dict[str, str]          # {vm_id: worker_name}
    solver_status: str                   # OPTIMAL | FEASIBLE | INFEASIBLE
    greedy_hint_used: bool = False
    detail: Optional[str] = None


class WorkerStatus(BaseModel):
    """Estado unificado de un worker (MariaDB + Prometheus)."""
    worker_id: str
    name: str
    zone: str
    ip: str
    cpu_total: int
    ram_total_gb: float
    disk_total_gb: float
    vcpus_comprometidos: int
    ram_comprometida_gb: float
    disk_comprometido_gb: float
    avg_cpu_uso: float       # 0.0 – 1.0 de Prometheus
    avg_ram_uso: float       # 0.0 – 1.0 de Prometheus
    disponible: bool
    # calculados
    cap_cpu: float = 0.0
    cap_ram_gb: float = 0.0
    cap_disk_gb: float = 0.0


# ---------------------------------------------------------------------------
# Capa de datos — MariaDB
# ---------------------------------------------------------------------------

def _get_db_connection():
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
    )


def get_workers_from_db(zone: Optional[str] = None, cluster: Optional[str] = None) -> list[dict]:
    """
    Lee servidor_fisico de MariaDB.

    Lógica de filtrado:
      - Si se especifica `zone`:          filtra exactamente por esa zona.
      - Si no hay `zone` pero sí `cluster`:
          · cluster="openstack" → solo zonas con prefijo "az-openstack"
          · cluster="linux"     → excluye zonas de OpenStack (cualquier cosa
                                    que NO empiece por "az-openstack")
      - Si no hay ni `zone` ni `cluster`: devuelve todos los workers activos.
    """
    conn = _get_db_connection()
    try:
        with conn.cursor() as cur:
            if zone:
                cur.execute(
                    "SELECT * FROM servidor_fisico WHERE activo=1 AND zona_disponibilidad=%s",
                    (zone,),
                )
            elif cluster == "openstack":
                cur.execute(
                    "SELECT * FROM servidor_fisico WHERE activo=1 "
                    "AND zona_disponibilidad LIKE 'az-openstack%'",
                )
            elif cluster == "linux":
                cur.execute(
                    "SELECT * FROM servidor_fisico WHERE activo=1 "
                    "AND zona_disponibilidad NOT LIKE 'az-openstack%'",
                )
            else:
                cur.execute("SELECT * FROM servidor_fisico WHERE activo=1")
            return cur.fetchall()
    finally:
        conn.close()


def update_worker_resources(assignments: dict[str, str], vms: list[VMSpec], conn=None, sign: int = 1):
    """
    Reserva atómica: actualiza vcpus_used, ram_used_mb, storage_used_gb
    en servidor_fisico para cada worker asignado.

    sign=+1  → confirmar (sumar recursos al comprometer un slice)
    sign=-1  → liberar  (restar recursos al borrar un slice)

    Usar `sign` en vez de pasar VMSpec con valores negativos evita
    que los validadores Pydantic rechacen los valores.
    """
    # Agrupa por worker
    worker_totals: dict[str, dict] = {}
    for vm in vms:
        worker_name = assignments[vm.vm_id]
        if worker_name not in worker_totals:
            worker_totals[worker_name] = {"cpu": 0, "ram_mb": 0, "disk_gb": 0}
        worker_totals[worker_name]["cpu"] += vm.cpu * sign
        worker_totals[worker_name]["ram_mb"] += int(vm.ram_gb * 1024) * sign
        worker_totals[worker_name]["disk_gb"] += vm.disk_gb * sign

    close_conn = conn is None
    if conn is None:
        conn = _get_db_connection()
    try:
        with conn.cursor() as cur:
            for worker_name, totals in worker_totals.items():
                cur.execute(
                    """
                    UPDATE servidor_fisico
                    SET vcpus_used       = vcpus_used + %s,
                        ram_used_mb      = ram_used_mb + %s,
                        storage_used_gb  = storage_used_gb + %s,
                        vms_activas      = vms_activas + %s
                    WHERE nombre = %s
                    """,
                    (
                        totals["cpu"],
                        totals["ram_mb"],
                        totals["disk_gb"],
                        sum(1 for v in vms if assignments[v.vm_id] == worker_name) * sign,
                        worker_name,
                    ),
                )
        conn.commit()
    finally:
        if close_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Capa de datos — Prometheus
# ---------------------------------------------------------------------------

async def get_prometheus_usage(worker_names: list[str]) -> dict[str, dict[str, float]]:
    """
    Consulta Prometheus para obtener avg_cpu_uso, avg_ram_uso, std_cpu_uso y
    std_ram_uso de los últimos PROMETHEUS_WINDOW minutos.

    """
    result: dict[str, dict[str, float]] = {
        name: {"avg_cpu": OC_FALLBACK, "avg_ram": OC_FALLBACK, "std_cpu": 0.0, "std_ram": 0.0}
        for name in worker_names
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            for worker_name in worker_names:
                # --- CPU: ventana 5 min, estimador histórico ---
                cpu_count_query = (
                    f'count_over_time(rate(node_cpu_seconds_total'
                    f'{{node="{worker_name}",mode="idle"}}[1m])[5m:30s])'
                )
                cpu_avg_query = (
                    f'1 - avg(avg_over_time(rate(node_cpu_seconds_total'
                    f'{{node="{worker_name}",mode="idle"}}[1m])[5m:30s]))'
                )
                cpu_std_query = (
                    f'stddev(avg_over_time(rate(node_cpu_seconds_total'
                    f'{{node="{worker_name}",mode="idle"}}[1m])[5m:30s]))'
                )
                # --- RAM: métrica instantánea, sin ventana histórica ---
                ram_inst_query = (
                    f'1 - (node_memory_MemAvailable_bytes{{node="{worker_name}"}}'
                    f' / node_memory_MemTotal_bytes{{node="{worker_name}"}})'
                )
                queries = [
                    ("cpu_count", cpu_count_query),
                    ("avg_cpu",   cpu_avg_query),
                    ("std_cpu",   cpu_std_query),
                    ("avg_ram",   ram_inst_query),
                ]

                for metric, query in queries:
                    resp = await client.get(
                        f"{PROMETHEUS_URL}/api/v1/query",
                        params={"query": query},
                    )
                    data = resp.json()
                    if data.get("status") == "success" and data["data"]["result"]:
                        value = float(data["data"]["result"][0]["value"][1])
                        if metric == "cpu_count":
                            # Cold start check: < 3 puntos → fallback ρ=1
                            if value < 3:
                                logger.warning(
                                    "Worker %s tiene historia insuficiente en Prometheus "
                                    "(%d puntos), aplicando fallback ρ=1", worker_name, int(value)
                                )
                                result[worker_name]["avg_cpu"] = OC_FALLBACK
                                result[worker_name]["std_cpu"] = 0.0
                        elif metric == "avg_cpu":
                            result[worker_name]["avg_cpu"] = max(0.01, min(1.0, value))
                        elif metric == "std_cpu":
                            result[worker_name]["std_cpu"] = max(0.0, value)
                        elif metric == "avg_ram":
                            # RAM instantánea: restricción dura, no promedio
                            result[worker_name]["avg_ram"] = max(0.01, min(1.0, value))
                            result[worker_name]["std_ram"] = 0.0  # sin varianza para RAM

    except Exception as exc:
        logger.warning("Prometheus no disponible, usando fallback ratio=1.0 sin penalización de varianza: %s", exc)

    return result


# ---------------------------------------------------------------------------
# Cálculo de capacidades efectivas (over-commitment dinámico)
# ---------------------------------------------------------------------------

def compute_effective_capacities(
    worker: dict, prom: dict[str, float]
) -> tuple[float, float, float]:
    """
    Calcula cap_cpu, cap_ram_gb, cap_disk_gb para un worker.

    El overcommit se ajusta por un estimador conservador de uso esperado:
    avg + k*std, no solo el promedio. Dos workers con la misma media pero
    distinta volatilidad reciben overcommit distinto — el más errático
    queda más limitado (ver sección 4.5 de Solucion_VM_Placement_v5.docx).

    En t=0 (sin compromisos ni historia): avg≈SO base (2-8%), std≈0,
    el sistema se comporta igual que sin este ajuste.
    """
    cpu_libre = worker["vcpus_total"] - worker["vcpus_used"]
    ram_libre_gb = (worker["ram_total_mb"] - worker["ram_used_mb"]) / 1024.0
    disk_libre_gb = worker["storage_total_gb"] - worker["storage_used_gb"]

    avg_cpu = prom.get("avg_cpu", OC_FALLBACK)
    avg_ram = prom.get("avg_ram", OC_FALLBACK)
    std_cpu = prom.get("std_cpu", 0.0)
    std_ram = prom.get("std_ram", 0.0)

    # Estimador conservador: media + k desviaciones estándar
    uso_estimado_cpu = avg_cpu + RISK_FACTOR_K * std_cpu
    uso_estimado_ram = avg_ram + RISK_FACTOR_K * std_ram

    ratio_cpu = min(MAX_OC_CPU, 1.0 / uso_estimado_cpu) if uso_estimado_cpu > 0.01 else 1.0
    ratio_ram = min(MAX_OC_RAM, 1.0 / uso_estimado_ram) if uso_estimado_ram > 0.01 else 1.0

    cap_cpu = cpu_libre * ratio_cpu
    cap_ram_gb = ram_libre_gb * ratio_ram
    cap_disk_gb = disk_libre_gb  # disco: Opción A, siempre exacto sin overcommit

    return cap_cpu, cap_ram_gb, cap_disk_gb


# ---------------------------------------------------------------------------
# Warm Start — Heurística Greedy Least Loaded
# ---------------------------------------------------------------------------

def greedy_least_loaded(
    vms: list[VMSpec],
    workers: list[WorkerStatus],
) -> Optional[dict[str, str]]:
    """
    Greedy: ordena VMs de mayor a menor (por cpu+ram+disk normalizado),
    asigna cada una al worker con más capacidad libre en ese momento.
    Sirve como warm start hint para CP-SAT y como fallback si OR-Tools falla.
    Devuelve None si no es factible.
    """
    # Copia mutable de capacidades libres
    free_cpu = {w.name: w.cap_cpu for w in workers}
    free_ram = {w.name: w.cap_ram_gb for w in workers}
    free_disk = {w.name: w.cap_disk_gb for w in workers}

    # Ordenar VMs de mayor a menor demanda total normalizada
    def vm_weight(vm: VMSpec) -> float:
        return vm.cpu + vm.ram_gb + vm.disk_gb

    sorted_vms = sorted(vms, key=vm_weight, reverse=True)
    assignments: dict[str, str] = {}

    for vm in sorted_vms:
        best_worker = None
        best_score = -1.0

        for w in workers:
            if (
                free_cpu[w.name] >= vm.cpu
                and free_ram[w.name] >= vm.ram_gb
                and free_disk[w.name] >= vm.disk_gb
            ):
                # Score: worker con más recursos libres combinados
                score = (
                    free_cpu[w.name] / max(w.cpu_total, 1)
                    + free_ram[w.name] / max(w.ram_total_gb, 0.001)
                    + free_disk[w.name] / max(w.disk_total_gb, 0.001)
                )
                if score > best_score:
                    best_score = score
                    best_worker = w.name

        if best_worker is None:
            return None  # INFEASIBLE en greedy también

        assignments[vm.vm_id] = best_worker
        free_cpu[best_worker] -= vm.cpu
        free_ram[best_worker] -= vm.ram_gb
        free_disk[best_worker] -= vm.disk_gb

    return assignments


# ---------------------------------------------------------------------------
# Solver CP-SAT — modelo matemático completo
# ---------------------------------------------------------------------------

def solve_placement(
    vms: list[VMSpec],
    workers: list[WorkerStatus],
    greedy_hint: Optional[dict[str, str]],
) -> tuple[str, dict[str, str]]:
    """
    Resuelve el problema de VM Placement con OR-Tools CP-SAT.

    Modelo:
      Variables:  x_ij ∈ {0,1}  (1 si VM i → worker j)
                  M_max, M_min ∈ [0, SCALE]  (auxiliares para linealizar max/min)

      Restricciones duras:
        R1: Σⱼ x_ij = 1                          ∀ i   (asignación única)
        R2: Σᵢ cpu(i)  · x_ij ≤ cap_cpu(j)      ∀ j
        R3: Σᵢ ram(i)  · x_ij ≤ cap_ram(j)      ∀ j
        R4: Σᵢ disk(i) · x_ij ≤ cap_disk(j)     ∀ j
        R5: M_max ≥ util_int(j)                   ∀ j
        R6: M_min ≤ util_int(j)                   ∀ j

      Objetivo:  Minimizar M_max - M_min  (balance de carga)

    Pesos dinámicos: calculados del slice para priorizar el recurso más demandado.
    Escala ×1000 porque CP-SAT solo trabaja con enteros.
    """
    model = cp_model.CpModel()

    n_vms = len(vms)
    n_workers = len(workers)

    # --- Pesos dinámicos (calculados del slice) ---
    total_cpu = sum(v.cpu for v in vms)
    total_ram = sum(v.ram_gb for v in vms)
    total_disk = sum(v.disk_gb for v in vms)
    total_sum = total_cpu + total_ram + total_disk

    if total_sum == 0:
        total_sum = 1  # guard

    W_CPU = int(total_cpu / total_sum * SCALE)
    W_RAM = int(total_ram / total_sum * SCALE)
    W_DISK = SCALE - W_CPU - W_RAM

    logger.info(
        "Pesos dinámicos — W_CPU=%d W_RAM=%d W_DISK=%d | VMs=%d Workers=%d",
        W_CPU, W_RAM, W_DISK, n_vms, n_workers,
    )

    # --- Variables de decisión x[i][j] ---
    x = [
        [model.new_bool_var(f"x_{i}_{j}") for j in range(n_workers)]
        for i in range(n_vms)
    ]

    # --- Variables auxiliares para linealizar max/min ---
    M_max = model.new_int_var(0, SCALE, "M_max")
    M_min = model.new_int_var(0, SCALE, "M_min")

    # --- R1: cada VM va a exactamente un worker ---
    for i in range(n_vms):
        model.add(sum(x[i][j] for j in range(n_workers)) == 1)

    # --- R2, R3, R4: capacidades por worker ---
    for j, worker in enumerate(workers):
        # CP-SAT trabaja en enteros — multiplicamos por 100 para preservar decimales
        cap_cpu_int = int(worker.cap_cpu * 100)
        cap_ram_int = int(worker.cap_ram_gb * 100)
        cap_disk_int = int(worker.cap_disk_gb * 100)

        model.add(
            sum(int(vms[i].cpu * 100) * x[i][j] for i in range(n_vms)) <= cap_cpu_int
        )
        model.add(
            sum(int(vms[i].ram_gb * 100) * x[i][j] for i in range(n_vms)) <= cap_ram_int
        )
        model.add(
            sum(int(vms[i].disk_gb * 100) * x[i][j] for i in range(n_vms)) <= cap_disk_int
        )

    # --- util_int(j): utilización normalizada ×SCALE por worker ---
    # --- util_int(j): utilización normalizada ×SCALE por worker ---
    util_int = []
    for j, worker in enumerate(workers):
        cpu_total_int = max(worker.cpu_total, 1)
        ram_total_int = max(int(worker.ram_total_gb * 100), 1)
        disk_total_int = max(int(worker.disk_total_gb * 100), 1)

        cpu_sum = sum(vms[i].cpu * x[i][j] for i in range(n_vms))
        ram_sum = sum(int(vms[i].ram_gb * 100) * x[i][j] for i in range(n_vms))
        disk_sum = sum(int(vms[i].disk_gb * 100) * x[i][j] for i in range(n_vms))

        # OR-Tools 9.10 no acepta una expresión directa como numerador en
        # add_division_equality ("expression must be affine"). Se requiere
        # una variable intermedia ligada con model.add() antes de dividir.
        max_cpu_num = W_CPU * sum(int(v.cpu) for v in vms)
        max_ram_num = W_RAM * sum(int(v.ram_gb * 100) for v in vms)
        max_disk_num = W_DISK * sum(int(v.disk_gb * 100) for v in vms)

        cpu_num = model.new_int_var(0, max_cpu_num, f"cpu_num_{j}")
        ram_num = model.new_int_var(0, max_ram_num, f"ram_num_{j}")
        disk_num = model.new_int_var(0, max_disk_num, f"disk_num_{j}")
        model.add(cpu_num == W_CPU * cpu_sum)
        model.add(ram_num == W_RAM * ram_sum)
        model.add(disk_num == W_DISK * disk_sum)

        cpu_term = model.new_int_var(0, SCALE, f"cpu_term_{j}")
        ram_term = model.new_int_var(0, SCALE, f"ram_term_{j}")
        disk_term = model.new_int_var(0, SCALE, f"disk_term_{j}")

        model.add_division_equality(cpu_term, cpu_num, cpu_total_int)
        model.add_division_equality(ram_term, ram_num, ram_total_int * 100)
        model.add_division_equality(disk_term, disk_num, disk_total_int * 100)

        util_j = model.new_int_var(0, SCALE * 3, f"util_{j}")
        model.add(util_j == cpu_term + ram_term + disk_term)
        util_int.append(util_j)

    # --- R5, R6: M_max y M_min ---
    for j in range(n_workers):
        model.add(M_max >= util_int[j])
        model.add(M_min <= util_int[j])

    # --- Objetivo: minimizar desequilibrio ---
    model.minimize(M_max - M_min)

    # --- Warm start: hint del greedy ---
    if greedy_hint:
        for i, vm in enumerate(vms):
            worker_name = greedy_hint.get(vm.vm_id)
            if worker_name:
                for j, worker in enumerate(workers):
                    hint_val = 1 if worker.name == worker_name else 0
                    model.add_hint(x[i][j], hint_val)

    # --- Resolver ---
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = MAX_TIME_SOLVER
    solver.parameters.num_search_workers = 4
    status = solver.solve(model)

    status_name = solver.status_name(status)
    logger.info("CP-SAT status=%s  objective=%s", status_name,
                solver.objective_value if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else "N/A")

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        assignments: dict[str, str] = {}
        for i, vm in enumerate(vms):
            for j, worker in enumerate(workers):
                if solver.value(x[i][j]) == 1:
                    assignments[vm.vm_id] = worker.name
                    break
        return status_name, assignments

    return status_name, {}


# ---------------------------------------------------------------------------
# Endpoint principal POST /place
# ---------------------------------------------------------------------------

@app.post("/place", response_model=PlacementResponse)
async def place(payload: PlacementRequest):
    """
    Asigna cada VM del slice a un worker físico usando CP-SAT.

    Flujo:
      1. Lee workers de MariaDB filtrados por zona
      2. Consulta Prometheus para avg_cpu y avg_ram
      3. Calcula capacidades efectivas (con over-commitment dinámico)
      4. Corre heurística Greedy como warm start
      5. Corre OR-Tools CP-SAT con el hint del greedy
      6. Retorna asignación {vm_id: worker_name}
    """
    # 1. Workers de MariaDB — filtrar por zona si se especificó, o por cluster
    try:
        db_workers = get_workers_from_db(zone=payload.zone, cluster=payload.cluster)
    except Exception as exc:
        logger.error("Error conectando a MariaDB: %s", exc)
        raise HTTPException(status_code=503, detail=f"Base de datos no disponible: {exc}")

    if not db_workers:
        raise HTTPException(
            status_code=400,
            detail=f"No hay workers disponibles para zona={payload.zone!r}",
        )

    worker_names = [w["nombre"] for w in db_workers]

    # 2. Datos de Prometheus
    prom_data = await get_prometheus_usage(worker_names)

    # 3. Capacidades efectivas
    workers: list[WorkerStatus] = []
    for w in db_workers:
        cap_cpu, cap_ram_gb, cap_disk_gb = compute_effective_capacities(
            w, prom_data.get(w["nombre"], {})
        )
        workers.append(WorkerStatus(
            worker_id=str(w["id"]),
            name=w["nombre"],
            zone=w["zona_disponibilidad"],
            ip=w["ip_interna"],
            cpu_total=w["vcpus_total"],
            ram_total_gb=w["ram_total_mb"] / 1024.0,
            disk_total_gb=w["storage_total_gb"],
            vcpus_comprometidos=w["vcpus_used"],
            ram_comprometida_gb=w["ram_used_mb"] / 1024.0,
            disk_comprometido_gb=w["storage_used_gb"],
            avg_cpu_uso=prom_data.get(w["nombre"], {}).get("avg_cpu", OC_FALLBACK),
            avg_ram_uso=prom_data.get(w["nombre"], {}).get("avg_ram", OC_FALLBACK),
            disponible=bool(w["activo"]),
            cap_cpu=cap_cpu,
            cap_ram_gb=cap_ram_gb,
            cap_disk_gb=cap_disk_gb,
        ))

    logger.info(
        "Workers disponibles: %s",
        [{w.name: {"cap_cpu": round(w.cap_cpu, 1), "cap_ram": round(w.cap_ram_gb, 1),
                   "cap_disk": round(w.cap_disk_gb, 1)}} for w in workers]
    )

    # 4. Warm start — heurística greedy
    greedy_hint = greedy_least_loaded(payload.vms, workers)
    greedy_used = greedy_hint is not None

    logger.info("Greedy hint: %s", greedy_hint)

    # 5. CP-SAT
    status_name, assignments = solve_placement(payload.vms, workers, greedy_hint)

    # 6. Manejar resultado
    if status_name == "INFEASIBLE":
        # Diagnóstico detallado del cuello de botella
        total_cpu_req = sum(v.cpu for v in payload.vms)
        total_ram_req = sum(v.ram_gb for v in payload.vms)
        total_disk_req = sum(v.disk_gb for v in payload.vms)
        total_cpu_cap = sum(w.cap_cpu for w in workers)
        total_ram_cap = sum(w.cap_ram_gb for w in workers)
        total_disk_cap = sum(w.cap_disk_gb for w in workers)

        bottlenecks = []
        if total_cpu_req > total_cpu_cap:
            bottlenecks.append(
                f"CPU: se piden {total_cpu_req} vCPUs, hay {total_cpu_cap:.1f} disponibles"
            )
        if total_ram_req > total_ram_cap:
            bottlenecks.append(
                f"RAM: se piden {total_ram_req:.1f} GB, hay {total_ram_cap:.1f} disponibles"
            )
        if total_disk_req > total_disk_cap:
            bottlenecks.append(
                f"Disco: se piden {total_disk_req:.1f} GB, hay {total_disk_cap:.1f} disponibles"
            )

        detail = "Recursos insuficientes. " + (
            " | ".join(bottlenecks) if bottlenecks
            else "Fragmentación: ningún worker individual puede alojar alguna VM."
        )

        raise HTTPException(status_code=409, detail=detail)

    if not assignments:
        # FEASIBLE con timeout pero sin solución (raro), usar greedy como fallback
        if greedy_hint:
            logger.warning("CP-SAT timeout sin solución, usando greedy como fallback")
            assignments = greedy_hint
            status_name = "GREEDY_FALLBACK"
        else:
            raise HTTPException(status_code=409, detail="No se encontró asignación válida")

    return PlacementResponse(
        success=True,
        assignments=assignments,
        solver_status=status_name,
        greedy_hint_used=greedy_used,
    )
class ReleaseRequest(BaseModel):
    assignments: dict[str, str]   # {vm_id: worker_name}
    vms: list[VMSpec]


@app.post("/release")
def release(payload: ReleaseRequest):
    """
    Libera recursos en MariaDB al borrar un slice.
    Pasa sign=-1 para restar en vez de crear VMSpec con valores negativos
    (que serían rechazados por los validadores Pydantic de VMSpec).
    """
    try:
        update_worker_resources(payload.assignments, payload.vms, sign=-1)
        return {"success": True, "released": len(payload.vms)}
    except Exception as exc:
        logger.error("Error liberando recursos: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/confirm")
def confirm(payload: ReleaseRequest):
    """
    Confirma reserva de recursos en MariaDB tras deploy exitoso.
    """
    try:
        update_worker_resources(payload.assignments, payload.vms)
        return {"success": True, "confirmed": len(payload.vms)}
    except Exception as exc:
        logger.error("Error confirmando recursos: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

# ---------------------------------------------------------------------------
# Endpoints auxiliares
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "solver": "CP-SAT OR-Tools", "version": "2.0.0"}


@app.get("/workers/status")
async def workers_status(zone: Optional[str] = None):
    """
    Expone el estado actual de todos los workers con capacidades efectivas.
    Útil para el monitoring_collector y para debugging.
    """
    try:
        db_workers = get_workers_from_db(zone=zone)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    worker_names = [w["nombre"] for w in db_workers]
    prom_data = await get_prometheus_usage(worker_names)

    result = []
    for w in db_workers:
        cap_cpu, cap_ram_gb, cap_disk_gb = compute_effective_capacities(
            w, prom_data.get(w["nombre"], {})
        )
        result.append({
            "name": w["nombre"],
            "zone": w["zona_disponibilidad"],
            "ip": w["ip_interna"],
            "cpu_total": w["vcpus_total"],
            "ram_total_gb": round(w["ram_total_mb"] / 1024.0, 2),
            "disk_total_gb": w["storage_total_gb"],
            "vcpus_used": w["vcpus_used"],
            "ram_used_gb": round(w["ram_used_mb"] / 1024.0, 2),
            "disk_used_gb": w["storage_used_gb"],
            "avg_cpu_uso": round(prom_data.get(w["nombre"], {}).get("avg_cpu", 0), 3),
            "avg_ram_uso": round(prom_data.get(w["nombre"], {}).get("avg_ram", 0), 3),
            "std_cpu_uso": round(prom_data.get(w["nombre"], {}).get("std_cpu", 0), 3),
            "std_ram_uso": round(prom_data.get(w["nombre"], {}).get("std_ram", 0), 3),
            "cap_cpu_efectiva": round(cap_cpu, 2),
            "cap_ram_gb_efectiva": round(cap_ram_gb, 2),
            "cap_disk_gb_efectiva": round(cap_disk_gb, 2),
            "activo": bool(w["activo"]),
        })

    return result