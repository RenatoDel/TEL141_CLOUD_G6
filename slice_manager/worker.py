from __future__ import annotations
"""
worker.py
---------
Worker RQ que consume la cola y ejecuta el LinuxDriver.

Ejecutar en server4:
    source ~/venv/bin/activate
    cd ~/slice_manager
    python worker.py
"""

import sys
import os
import logging

# Agregar linux_driver al path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'linux_driver'))

import redis
from rq import Worker, Queue

from database import SessionLocal
from models import Job, Slice, VM, EstadoJobEnum, EstadoSliceEnum, EstadoVMEnum

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

REDIS_URL = "redis://localhost:6379"
QUEUE_NAME = "slice_jobs"


def ejecutar_create_slice(job_uid: str):
    """
    Ejecuta la creación de un slice.
    Llamado por RQ desde la cola.
    """
    db = SessionLocal()
    try:
        # Obtener job y slice de MySQL
        job = db.query(Job).filter(Job.job_uid == job_uid).first()
        if not job:
            logger.error(f"Job {job_uid} no encontrado")
            return

        slice_obj = job.slice
        logger.info(f"[Worker] Ejecutando job {job_uid} — slice {slice_obj.slice_uid}")

        # Marcar job como running
        job.estado = EstadoJobEnum.running
        _update_step(job, 1, "running")  # VM Placement
        db.commit()

        # Importar driver
        from driver import LinuxDriver, SliceRequest

        # Construir SliceRequest desde MySQL
        servers = [vm.servidor.nombre for vm in slice_obj.vms]
        vnc_ports = [vm.vnc_port for vm in slice_obj.vms]

        request = SliceRequest(
            slice_id     = slice_obj.slice_uid,
            topology     = slice_obj.topologia.nombre,
            vlan_id      = slice_obj.vlan_id,
            cidr         = slice_obj.cidr,
            vm_count     = len(slice_obj.vms),
            servers      = servers,
            vnc_start    = vnc_ports[0],
            has_internet = slice_obj.tiene_internet,
            has_dhcp     = slice_obj.tiene_dhcp,
        )

        # VM Placement completado
        _update_step(job, 1, "done")
        _update_step(job, 2, "running")  # Configurando red VLAN
        db.commit()

        # Ejecutar driver
        driver = LinuxDriver(ssh_mode="internal")
        result = driver.create_slice(request)

        if not result.success:
            raise RuntimeError(result.error or "Error desconocido en el driver")

        # Configurando red completada
        _update_step(job, 2, "done")
        _update_step(job, 3, "running")  # Creando VMs
        db.commit()

        # Actualizar VMs en MySQL con estado real
        for vm_result in result.vms:
            logger.info(f"[Worker] Actualizando VM vm_id={vm_result.vm_id} status={vm_result.status}")
            vm = db.query(VM).filter(VM.vm_uid == vm_result.vm_id).first()
            if vm:
                vm.estado = EstadoVMEnum.running if vm_result.status == "running" else EstadoVMEnum.error
                logger.info(f"[Worker] VM {vm.vm_uid} actualizada a {vm.estado}")
            else:    
                logger.warning(f"[Worker] VM {vm_result.vm_id} no encontrada en MySQL")
        _update_step(job, 3, "done")
        _update_step(job, 4, "running")  # Verificando estado
        db.commit()

        _update_step(job, 4, "done")

        # Marcar slice y job como completados
        slice_obj.estado = EstadoSliceEnum.running
        job.estado       = EstadoJobEnum.completed
        db.commit()

        logger.info(f"[Worker] Slice {slice_obj.slice_uid} desplegado exitosamente")

    except Exception as e:
        logger.error(f"[Worker] Error en job {job_uid}: {e}")
        if job:
            job.estado = EstadoJobEnum.failed
            job.error  = str(e)
        if slice_obj:
            slice_obj.estado = EstadoSliceEnum.error
        db.commit()

    finally:
        db.close()


def ejecutar_delete_slice(job_uid: str):
    """
    Ejecuta el borrado de un slice.
    Llamado por RQ desde la cola.
    """
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_uid == job_uid).first()
        if not job:
            logger.error(f"Job {job_uid} no encontrado")
            return

        slice_obj = job.slice
        logger.info(f"[Worker] Borrando slice {slice_obj.slice_uid}")

        job.estado = EstadoJobEnum.running
        db.commit()

        from driver import LinuxDriver

        vms = [
            {
                "name":     vm.nombre,
                "server":   vm.servidor.nombre,
                "vm_id":    vm.vm_uid,
                "vnc_port": vm.vnc_port,
            }
            for vm in slice_obj.vms
        ]

        driver  = LinuxDriver(ssh_mode="internal")
        success = driver.delete_slice(
            slice_obj.slice_uid,
            slice_obj.vlan_id,
            slice_obj.cidr,
            vms,
        )

        if success:
            slice_obj.estado = EstadoSliceEnum.deleted
            job.estado       = EstadoJobEnum.completed
        else:
            slice_obj.estado = EstadoSliceEnum.error
            job.estado       = EstadoJobEnum.failed
            job.error        = "Error al borrar recursos en el cluster"

        db.commit()
        logger.info(f"[Worker] Slice {slice_obj.slice_uid} borrado — success={success}")

    except Exception as e:
        logger.error(f"[Worker] Error borrando slice: {e}")
        if job:
            job.estado = EstadoJobEnum.failed
            job.error  = str(e)
        db.commit()

    finally:
        db.close()


def _update_step(job: Job, step_index: int, status: str):
    """Actualiza el estado de un paso en el progreso del job."""
    if job.progreso and "steps" in job.progreso:
        steps = job.progreso["steps"]
        if step_index < len(steps):
            steps[step_index]["status"] = status
            job.progreso = {"steps": steps}


if __name__ == "__main__":
    conn = redis.from_url(REDIS_URL)
    q = Queue(QUEUE_NAME, connection=conn)
    worker = Worker([q], connection=conn)
    logger.info(f"[Worker] Escuchando cola '{QUEUE_NAME}'...")
    worker.work()