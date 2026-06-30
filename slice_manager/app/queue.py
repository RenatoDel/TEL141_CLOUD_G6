"""
Módulo de colas para despliegue/borrado asíncrono de slices.

Centraliza la conexión a Redis y la cola "slices" usada tanto por la
API (encolar) como por el worker (ejecutar). Mantiene la firma de
graph_orchestrator intacta: las funciones que se encolan son wrappers
delgados que llaman al orquestador existente.
"""
from __future__ import annotations

import os
import logging

import redis
from rq import Queue
from rq.job import Job

logger = logging.getLogger("slice_manager.queue")

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

_redis_conn = redis.from_url(REDIS_URL)
slice_queue = Queue("slices", connection=_redis_conn, default_timeout=600)
# default_timeout=600s (10 min): cubre el caso peor de un slice grande
# en OpenStack con varias VMs esperando ACTIVE. Ajustable si tus
# topologías de prueba son más pesadas.


def get_job_status(job_id: str) -> dict:
    """Consulta el estado de un job por su ID. Usado por el endpoint
    de polling GET /graph-slices/{slice_name}/job-status."""
    try:
        job = Job.fetch(job_id, connection=_redis_conn)
    except Exception:
        return {"status": "not_found", "job_id": job_id}

    status = job.get_status()  # queued | started | finished | failed | deferred
    payload = {"status": status, "job_id": job_id}

    if status == "finished":
        payload["result"] = job.result
    elif status == "failed":
        # job.exc_info contiene el traceback completo; solo exponemos
        # un resumen al cliente, el resto va a logs del worker.
        payload["error"] = (
            str(job.exc_info).strip().splitlines()[-1] if job.exc_info else "Error desconocido"
        )

    return payload
