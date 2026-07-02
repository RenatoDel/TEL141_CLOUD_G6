"""
Worker de reconciliación periódica.
Sincroniza vcpus_used, ram_used_mb, storage_used_gb en servidor_fisico
con el estado real según slices.json cada 5 minutos.
"""
from __future__ import annotations
import json
import logging
import os
import time
import pymysql
import pymysql.cursors

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reconcile")

DB_HOST = os.getenv("DB_HOST", "mariadb")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "pucp")
DB_PASS = os.getenv("DB_PASS", "pucp_pass")
DB_NAME = os.getenv("DB_NAME", "pucp_cloud")
INTERVAL = int(os.getenv("RECONCILE_INTERVAL", "300"))
SLICES_PATH = os.getenv("SLICES_JSON_PATH", "/app/state/slices.json")


def _get_conn():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASS, database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor, connect_timeout=5,
    )


def read_slices() -> list[dict]:
    try:
        with open(SLICES_PATH) as f:
            return json.load(f)
    except Exception as e:
        logger.warning("No se pudo leer slices.json: %s", e)
        return []


def compute_real_usage(slices: list[dict]) -> dict[str, dict]:
    """
    Suma los recursos de todas las VMs activas por worker
    leyendo slices.json. Estructura del slice:
      - slice["workers"]  = {vm_id: worker_name}
      - slice["nodes"]    = [{name, vcpus, ram_mb, disk_gb, ...}]
    """
    usage: dict[str, dict] = {}

    for s in slices:
        workers_map = s.get("workers", {})   # {vm_id: worker_name}
        nodes = {n["name"]: n for n in s.get("nodes", [])}

        for vm_id, worker_name in workers_map.items():
            if worker_name not in usage:
                usage[worker_name] = {
                    "vcpus_used": 0,
                    "ram_used_mb": 0,
                    "storage_used_gb": 0,
                    "vms_activas": 0,
                }
            node = nodes.get(vm_id, {})
            usage[worker_name]["vcpus_used"]      += node.get("vcpus", 1)
            usage[worker_name]["ram_used_mb"]     += node.get("ram_mb", 512)
            usage[worker_name]["storage_used_gb"] += node.get("disk_gb", 4)
            usage[worker_name]["vms_activas"]     += 1

    return usage


def reconcile():
    logger.info("Iniciando ciclo de reconciliación...")
    slices = read_slices()
    real_usage = compute_real_usage(slices)

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT nombre, vcpus_used, ram_used_mb, storage_used_gb, vms_activas FROM servidor_fisico WHERE activo=1")
            workers = cur.fetchall()

        for w in workers:
            name = w["nombre"]
            real = real_usage.get(name, {
                "vcpus_used": 0, "ram_used_mb": 0,
                "storage_used_gb": 0, "vms_activas": 0
            })

            drift = {k: abs(real[k] - w[k]) for k in real}
            if any(v > 0 for v in drift.values()):
                logger.warning(
                    "DRIFT en %s — BD: cpu=%s ram=%s disk=%s vms=%s | "
                    "Real: cpu=%s ram=%s disk=%s vms=%s",
                    name,
                    w["vcpus_used"], w["ram_used_mb"],
                    w["storage_used_gb"], w["vms_activas"],
                    real["vcpus_used"], real["ram_used_mb"],
                    real["storage_used_gb"], real["vms_activas"],
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
                logger.info("Worker %s reconciliado correctamente.", name)
            else:
                logger.info("Worker %s OK — sin drift.", name)

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