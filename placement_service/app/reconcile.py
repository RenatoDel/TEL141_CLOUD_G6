"""
Worker de reconciliación periódica.
Sincroniza vcpus_used, ram_used_mb, storage_used_gb en servidor_fisico
con el estado real del cluster cada 5 minutos.
Corre como proceso independiente (no como endpoint FastAPI).
"""
from __future__ import annotations
import logging
import os
import time
import subprocess
import pymysql
import pymysql.cursors

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reconcile")

DB_HOST = os.getenv("DB_HOST", "mariadb")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "pucp")
DB_PASS = os.getenv("DB_PASS", "pucp_pass")
DB_NAME = os.getenv("DB_NAME", "pucp_cloud")
INTERVAL = int(os.getenv("RECONCILE_INTERVAL", "300"))  # segundos


def _get_conn():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASS, database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor, connect_timeout=5,
    )


def get_workers() -> list[dict]:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT nombre, zona_disponibilidad FROM servidor_fisico WHERE activo=1")
            return cur.fetchall()
    finally:
        conn.close()


def get_real_usage_linux(worker_name: str, ssh_host: str, ssh_port: int) -> dict | None:
    """
    Consulta el worker Linux vía SSH y devuelve el uso real de recursos.
    Cuenta procesos QEMU activos para vcpus y ram.
    """
    try:
        cmd = [
            "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
            "-p", str(ssh_port), f"ubuntu@{ssh_host}",
            "virsh list --all --name 2>/dev/null | grep -v '^$' | wc -l; "
            "cat /proc/meminfo | grep MemAvailable | awk '{print $2}'; "
            "df / --output=used -BG | tail -1 | tr -d 'G '"
        ]
        out = subprocess.check_output(cmd, timeout=10, text=True).strip().split("\n")
        vms_activas = int(out[0])
        return {"vms_activas": vms_activas}
    except Exception as e:
        logger.warning("No se pudo consultar worker Linux %s: %s", worker_name, e)
        return None


def update_from_slices_json(worker_name: str, conn) -> dict | None:
    """
    Lee slices.json (montado en el contenedor) y suma los recursos
    de todas las VMs activas asignadas a este worker.
    """
    import json
    slices_path = os.getenv("SLICES_JSON_PATH", "/app/state/slices.json")
    try:
        with open(slices_path) as f:
            slices = json.load(f)
    except Exception as e:
        logger.warning("No se pudo leer slices.json: %s", e)
        return None

    cpu_total = 0
    ram_total_mb = 0
    disk_total_gb = 0
    vms = 0

    for slice_data in slices.values():
        assignments = slice_data.get("assignments", {})
        nodes = {n["name"]: n for n in slice_data.get("nodes", [])}
        for vm_id, assigned_worker in assignments.items():
            if assigned_worker == worker_name:
                node = nodes.get(vm_id, {})
                cpu_total   += node.get("vcpus", 1)
                ram_total_mb += node.get("ram_mb", 512)
                disk_total_gb += node.get("disk_gb", 4)
                vms += 1

    return {
        "vcpus_used":      cpu_total,
        "ram_used_mb":     ram_total_mb,
        "storage_used_gb": disk_total_gb,
        "vms_activas":     vms,
    }


def reconcile():
    logger.info("Iniciando ciclo de reconciliación...")
    workers = get_workers()
    conn = _get_conn()
    try:
        for w in workers:
            name = w["nombre"]
            real = update_from_slices_json(name, conn)
            if real is None:
                logger.warning("Sin datos para worker %s, omitiendo.", name)
                continue

            with conn.cursor() as cur:
                # Leer estado actual en BD
                cur.execute(
                    "SELECT vcpus_used, ram_used_mb, storage_used_gb, vms_activas "
                    "FROM servidor_fisico WHERE nombre=%s", (name,)
                )
                row = cur.fetchone()

            if row:
                drift = {k: abs(real[k] - row[k]) for k in real}
                if any(v > 0 for v in drift.values()):
                    logger.warning(
                        "DRIFT detectado en %s — BD: %s | Real: %s | Diff: %s",
                        name, dict(row), real, drift
                    )
                    with conn.cursor() as cur:
                        cur.execute(
                            """UPDATE servidor_fisico
                               SET vcpus_used=%s, ram_used_mb=%s,
                                   storage_used_gb=%s, vms_activas=%s
                               WHERE nombre=%s""",
                            (real["vcpus_used"], real["ram_used_mb"],
                             real["storage_used_gb"], real["vms_activas"], name)
                        )
                    conn.commit()
                    logger.info("Worker %s reconciliado.", name)
                else:
                    logger.info("Worker %s OK, sin drift.", name)
    finally:
        conn.close()


if __name__ == "__main__":
    logger.info("Reconciliation worker arrancado. Intervalo: %ds", INTERVAL)
    while True:
        try:
            reconcile()
        except Exception as e:
            logger.error("Error en ciclo de reconciliación: %s", e)
        time.sleep(INTERVAL)