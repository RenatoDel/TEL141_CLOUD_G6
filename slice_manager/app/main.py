from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from app.queue import slice_queue, get_job_status
from app.graph_orchestrator import run_create_graph_slice_job, run_delete_graph_slice_job

from .auth import require_token
from .graph_orchestrator import GraphOrchestrator
from .graph_schemas import (
    GraphSliceCloneRequest,
    GraphSliceCreateRequest,
    GraphSliceImportRequest,
)
from .orchestrator import Orchestrator
from .rbac import (
    assert_can_act,
    assert_can_view,
    current_user,
    filter_slices_for_user,
    require_role,
    require_write_access,
    resolve_owner_for_create,
)
from .schemas import SliceCreateRequest
from .state_store import (
    add_slice,
    delete_slice as remove_slice,
    get_slice,
    list_slices,
    replace_slice,
    next_free_vlan_base,
)

app = FastAPI(title="PUCP Slice Manager", version="0.6.0")

orchestrator = Orchestrator()
graph_orchestrator = GraphOrchestrator()


class VMActionRequest(BaseModel):
    action: str


# ════════════════════════════════════════════════════════════════════════════
# Health
# ════════════════════════════════════════════════════════════════════════════
@app.get("/health")
def health():
    return {"status": "ok"}


# ════════════════════════════════════════════════════════════════════════════
# Monitoring summary — visible para todos los roles autenticados
# ════════════════════════════════════════════════════════════════════════════
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://10.0.10.4:9090").rstrip("/")


async def _prom_query(expr: str):
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": expr})
        r.raise_for_status()
        payload = r.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"Prometheus query falló: {expr}")
        return payload.get("data", {}).get("result", [])


def _vector_to_node_map(items):
    out = {}
    for item in items:
        metric = item.get("metric", {})
        node = metric.get("node") or metric.get("instance") or "unknown"
        value = item.get("value", [None, "0"])[1]
        try:
            out[node] = float(value)
        except Exception:
            out[node] = 0.0
    return out


@app.get("/monitoring/summary")
async def monitoring_summary(_user=Depends(current_user)):
    queries = {
        "up": 'up{job=~"node_exporter_.*"}',
        "cpu": '100 * (1 - avg by (node) (rate(node_cpu_seconds_total{mode="idle"}[5m])))',
        "mem_total": "node_memory_MemTotal_bytes",
        "mem_avail": "node_memory_MemAvailable_bytes",
        "disk_total": 'node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay|squashfs"}',
        "disk_avail": 'node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay|squashfs"}',
    }

    results = {}
    for key, expr in queries.items():
        try:
            results[key] = _vector_to_node_map(await _prom_query(expr))
        except Exception:
            results[key] = {}

    # Lista real de workers monitoreados, según la topología del proyecto
    # (ver README sección 2 y diagrama de Fase 2): 3 workers Linux + 3
    # workers OpenStack. Etiquetamos cada uno con su cluster para que la
    # UI pueda separar por sección.
    wanted = [
        ("server1", "linux"),
        ("server2", "linux"),
        ("server3", "linux"),
        ("worker1", "openstack"),
        ("worker2", "openstack"),
        ("worker3", "openstack"),
    ]
    # Leer recursos reservados de MariaDB para cada worker.
    # vcpus_used, ram_used_mb, storage_used_gb = lo que el orquestador comprometió.
    # Distinto del uso físico real que reporta Prometheus.
    db_resources = {}
    try:
        import pymysql, os as _os
        conn = pymysql.connect(
            host=_os.getenv("DB_HOST", "mariadb"),
            port=int(_os.getenv("DB_PORT", 3306)),
            user=_os.getenv("DB_USER", "pucp"),
            password=_os.getenv("DB_PASS", "pucp_pass"),
            database=_os.getenv("DB_NAME", "pucp_cloud"),
        )
        with conn.cursor() as cur:
            cur.execute(
                """SELECT nombre, vcpus_total, vcpus_used,
                          ram_total_mb, ram_used_mb,
                          storage_total_gb, storage_used_gb
                   FROM servidor_fisico WHERE activo=1"""
            )
            for row in cur.fetchall():
                db_resources[row[0]] = {
                    "vcpus_total":       row[1],
                    "vcpus_reserved":    row[2],
                    "ram_total_mb":      row[3],
                    "ram_reserved_mb":   row[4],
                    "disk_total_gb":     row[5],
                    "disk_reserved_gb":  row[6],
                }
        conn.close()
    except Exception as e:
        import logging; logging.getLogger(__name__).warning("No se pudo leer recursos reservados de MariaDB: %s", e)
    db_disk = {k: {"total": v["disk_total_gb"], "reserved": v["disk_reserved_gb"]}
               for k, v in db_resources.items()}

    workers = []
    for node, cluster in wanted:
        mem_total = results["mem_total"].get(node, 0.0)
        mem_avail = results["mem_avail"].get(node, 0.0)
        disk_total = results["disk_total"].get(node, 0.0)
        disk_avail = results["disk_avail"].get(node, 0.0)

        mem_used = max(mem_total - mem_avail, 0.0)
        disk_used = max(disk_total - disk_avail, 0.0)

        # Disco reservado según MariaDB (lo que el orquestador comprometió)
        db_d = db_disk.get(node, {})
        disk_reserved_gb = float(db_d.get("reserved", 0))
        disk_capacity_gb = float(db_d.get("total", round(disk_total / (1024 ** 3), 2)))

        db = db_resources.get(node, {})
        workers.append({
            "worker": node,
            "cluster": cluster,
            "status": "up" if results["up"].get(node, 0.0) >= 1 else "down",
            "cpu_percent": round(results["cpu"].get(node, 0.0), 2),
            "mem_total_gb": round(mem_total / (1024 ** 3), 2),
            "mem_used_gb": round(mem_used / (1024 ** 3), 2),
            "mem_free_gb": round(mem_avail / (1024 ** 3), 2),
            "disk_total_gb": round(disk_total / (1024 ** 3), 2),
            "disk_used_gb": round(disk_used / (1024 ** 3), 2),
            "disk_free_gb": round(disk_avail / (1024 ** 3), 2),
            # Recursos reservados por el orquestador (de MariaDB)
            "vcpus_total":      db.get("vcpus_total"),
            "vcpus_reserved":   db.get("vcpus_reserved", 0),
            "ram_reserved_mb":  db.get("ram_reserved_mb", 0),
            "disk_reserved_gb": disk_reserved_gb,
            "disk_capacity_gb": disk_capacity_gb,
        })

    def _totals_for(ws):
        ups = [w for w in ws if w["status"] == "up"]
        return {
            "workers_total": len(ws),
            "workers_up": len(ups),
            "mem_total_gb": round(sum(w["mem_total_gb"] for w in ws), 2),
            "mem_used_gb": round(sum(w["mem_used_gb"] for w in ws), 2),
            "disk_total_gb": round(sum(w["disk_total_gb"] for w in ws), 2),
            "disk_used_gb": round(sum(w["disk_used_gb"] for w in ws), 2),
            "avg_cpu_percent": round(
                sum(w["cpu_percent"] for w in ups) / max(len(ups), 1), 2
            ),
        }

    linux_workers = [w for w in workers if w["cluster"] == "linux"]
    openstack_workers = [w for w in workers if w["cluster"] == "openstack"]

    return {
        "workers": workers,
        "totals": _totals_for(workers),
        "totals_by_cluster": {
            "linux": _totals_for(linux_workers),
            "openstack": _totals_for(openstack_workers),
        },
    }


@app.get("/monitoring/courses-summary")
def monitoring_courses_summary(user: dict = Depends(current_user)):
    """
    Resumen de slices agrupado por curso, para vista de profesor/coach.

    Por slice incluye: VMs totales, VMs activas, vCPUs/RAM/disco reservados
    (sumados de los nodos del slice). No expone workers físicos: los coaches
    auditan SUS cursos sin acceso al monitoreo del fierro.

    Filtrado por RBAC:
      - admin: ve todos los cursos con slices
      - profesor: solo cursos que dicta
      - coach: solo cursos que audita
      - alumno: solo SUS slices (caso especial: lista plana sin agrupar)
    """
    all_graph = [s for s in list_slices() if s.get("mode") == "graph"]
    visible = filter_slices_for_user(user, all_graph)

    def _vm_active(vm):
        st = (vm.get("status") or "").lower()
        return st in ("active", "running")

    def _slice_stats(s):
        vms = s.get("vms", [])
        return {
            "slice_name": s["slice_name"],
            "cluster": s.get("cluster", "linux"),
            "owner_username": s.get("owner_username"),
            "curso_id": s.get("curso_id"),
            "vm_count": len(vms),
            "vm_active": sum(1 for vm in vms if _vm_active(vm)),
            "vcpus_reserved": sum(int(vm.get("vcpus") or 0) for vm in vms),
            "ram_mb_reserved": sum(int(vm.get("ram_mb") or 0) for vm in vms),
            "disk_gb_reserved": sum(int(vm.get("disk_gb") or 0) for vm in vms),
        }

    # Agrupar por curso_id (None = sin curso asignado)
    by_course: dict = {}
    for s in visible:
        cid = s.get("curso_id")
        key = cid if cid is not None else "sin_curso"
        by_course.setdefault(key, []).append(_slice_stats(s))

    courses = []
    for cid, slist in by_course.items():
        courses.append({
            "curso_id": None if cid == "sin_curso" else cid,
            "slices": slist,
            "totals": {
                "slices": len(slist),
                "vms": sum(s["vm_count"] for s in slist),
                "vms_active": sum(s["vm_active"] for s in slist),
                "vcpus_reserved": sum(s["vcpus_reserved"] for s in slist),
                "ram_mb_reserved": sum(s["ram_mb_reserved"] for s in slist),
                "disk_gb_reserved": sum(s["disk_gb_reserved"] for s in slist),
            },
        })

    return {"role": user["role"], "courses": courses}


# ════════════════════════════════════════════════════════════════════════════
# Legacy slices (linear/ring) — mantenidos por compatibilidad
# ════════════════════════════════════════════════════════════════════════════
@app.get("/slices")
def get_slices(user: dict = Depends(current_user)):
    all_slices = list_slices()
    return filter_slices_for_user(user, all_slices)


@app.post("/slices")
async def create_slice(
    payload: SliceCreateRequest,
    user: dict = Depends(require_write_access),
):
    if payload.topology not in {"linear", "ring"}:
        raise HTTPException(status_code=400, detail="Topología no soportada por ahora")
    if payload.topology == "ring" and payload.vm_count < 3:
        raise HTTPException(status_code=400, detail="Ring requiere mínimo 3 VMs")

    if any(s["slice_name"] == payload.slice_name for s in list_slices()):
        raise HTTPException(status_code=409, detail="Ya existe un slice con ese nombre")

    owner_username, curso_id = resolve_owner_for_create(
        user, payload.owner_username, payload.curso_id
    )

    execution = await orchestrator.create_slice(payload)
    if not execution["result"]["success"]:
        raise HTTPException(
            status_code=400,
            detail=execution["result"].get("error") or "Error creando slice legacy",
        )

    stored = {
        "mode": "legacy",
        "slice_name": payload.slice_name,
        "topology": payload.topology,
        "vlan_id": payload.vlan_id,
        "cidr": payload.cidr,
        "vm_count": payload.vm_count,
        "workers": execution["workers"],
        "vms": execution["result"]["vms"],
        "deploy_mode": execution["deploy_mode"],
        "image_name": payload.image_name,
        # ─── Ownership ─────────────────────────────────────────────────
        "owner_username": owner_username,
        "owner_uid": user["uid"] if owner_username == user["sub"] else None,
        "curso_id": curso_id,
        "created_by": user["sub"],
    }
    add_slice(stored)
    return execution


@app.delete("/slices/{slice_name}")
async def delete_slice(slice_name: str, user: dict = Depends(require_write_access)):
    found = get_slice(slice_name)
    if not found:
        raise HTTPException(status_code=404, detail="Slice no encontrado")
    assert_can_act(user, found)

    remove_slice(slice_name)
    result = await orchestrator.delete_slice(slice_name, found)
    return {"slice_name": slice_name, "result": result}


# ════════════════════════════════════════════════════════════════════════════
# Graph slices (topologías arbitrarias) — borradores + biblioteca + cola RQ
# ════════════════════════════════════════════════════════════════════════════

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_slice_name_available(slice_name: str, *, ignore_name: str | None = None) -> None:
    for item in list_slices():
        current = item.get("slice_name")
        if current == slice_name and current != ignore_name:
            raise HTTPException(status_code=409, detail="Ya existe un slice con ese nombre")


def _payload_topology(payload: GraphSliceCreateRequest) -> dict:
    raw = payload.model_dump(by_alias=True)
    raw.pop("owner_username", None)
    raw.pop("curso_id", None)
    return raw


def _draft_record(
    payload: GraphSliceCreateRequest,
    *,
    owner_username: str,
    owner_uid,
    curso_id: int | None,
    created_by: str,
    created_at: str | None = None,
) -> dict:
    topology = _payload_topology(payload.model_copy(update={"vlan_base": None}))
    return {
        "mode": "graph",
        "state": "draft",
        **topology,
        "owner_username": owner_username,
        "owner_uid": owner_uid,
        "curso_id": curso_id,
        "created_by": created_by,
        "created_at": created_at or _utcnow(),
        "updated_at": _utcnow(),
    }


def _queued_record(
    payload: GraphSliceCreateRequest,
    *,
    owner_username: str,
    owner_uid,
    curso_id: int | None,
    created_by: str,
    created_at: str | None = None,
) -> dict:
    return {
        "mode": "graph",
        "state": "queued",
        **_payload_topology(payload),
        "owner_username": owner_username,
        "owner_uid": owner_uid,
        "curso_id": curso_id,
        "created_by": created_by,
        "created_at": created_at or _utcnow(),
        "updated_at": _utcnow(),
    }


def _normalise_topology_from_slice(found: dict, *, new_name: str | None = None) -> dict:
    """Convierte un draft o slice activo a GraphSliceCreateRequest portable."""
    cluster = found.get("cluster", "linux")
    source_nodes = found.get("nodes") or found.get("vms") or []
    nodes = []
    for node in source_nodes:
        nodes.append({
            "name": node.get("name") or node.get("vm_id"),
            "image_name": node.get("image_name") or ("cirros" if cluster == "openstack" else "cirros-base.img"),
            "vcpus": int(node.get("vcpus") or 1),
            "ram_mb": int(node.get("ram_mb") or 256),
            "disk_gb": int(node.get("disk_gb") or 10),
            "preferred_worker": node.get("preferred_worker"),
            "internet": bool(node.get("internet", False)),
        })

    links = []
    for idx, link in enumerate(found.get("links") or []):
        from_node = link.get("from") or link.get("from_node") or link.get("node_a")
        to_node = link.get("to") or link.get("to_node") or link.get("node_b")
        if from_node and to_node:
            links.append({
                "id": link.get("id") or f"link{idx + 1}",
                "from": from_node,
                "to": to_node,
            })

    topology = {
        "slice_name": new_name or found["slice_name"],
        # La VLAN física no se exporta/reserva: se reasigna al desplegar.
        "vlan_base": None,
        "vnc_start": int(found.get("vnc_start") or 5901),
        "network_backend": found.get("network_backend", "vlan"),
        "internet_mode": found.get("internet_mode", "none"),
        "cluster": cluster,
        "availability_zone": found.get("availability_zone"),
        "nodes": nodes,
        "links": links,
    }
    # Valida y normaliza antes de entregar/exportar/clonar.
    return GraphSliceCreateRequest(**topology).model_dump(by_alias=True)


def _assign_vlan_for_deploy(
    payload: GraphSliceCreateRequest,
    *,
    ignore_name: str | None = None,
) -> GraphSliceCreateRequest:
    if payload.vlan_base is None:
        try:
            assigned_vlan = next_free_vlan_base(len(payload.links))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return payload.model_copy(update={"vlan_base": assigned_vlan})

    requested_top = payload.vlan_base + len(payload.links) - 1
    for item in list_slices():
        if item.get("slice_name") == ignore_name:
            continue
        existing_base = item.get("vlan_base")
        if not existing_base:
            continue
        existing_links = len(item.get("links") or [])
        existing_top = existing_base + max(existing_links - 1, 0)
        if not (requested_top < existing_base or payload.vlan_base > existing_top):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"La VLAN base {payload.vlan_base} solapa con el slice "
                    f"'{item['slice_name']}' (VLANs {existing_base}–{existing_top})."
                ),
            )
    return payload


def _enqueue_create_job(
    payload: GraphSliceCreateRequest,
    *,
    owner_username: str,
    owner_uid,
    curso_id: int | None,
    created_by: str,
):
    return slice_queue.enqueue(
        run_create_graph_slice_job,
        payload.model_dump(),
        owner_username,
        owner_uid,
        curso_id,
        created_by,
        job_id=f"create-{payload.slice_name}",
    )


@app.get("/graph-slices")
def get_graph_slices(user: dict = Depends(current_user)):
    all_graph = [s for s in list_slices() if s.get("mode") == "graph"]
    return filter_slices_for_user(user, all_graph)


@app.post("/graph-slices/drafts", status_code=201)
async def create_graph_slice_draft(
    payload: GraphSliceCreateRequest,
    user: dict = Depends(require_write_access),
):
    _ensure_slice_name_available(payload.slice_name)
    owner_username, curso_id = resolve_owner_for_create(
        user, payload.owner_username, payload.curso_id
    )
    owner_uid = user["uid"] if owner_username == user["sub"] else None
    payload = payload.model_copy(update={"vlan_base": None})
    record = _draft_record(
        payload,
        owner_username=owner_username,
        owner_uid=owner_uid,
        curso_id=curso_id,
        created_by=user["sub"],
    )
    try:
        add_slice(record)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return record


@app.put("/graph-slices/drafts/{slice_name}")
async def update_graph_slice_draft(
    slice_name: str,
    payload: GraphSliceCreateRequest,
    user: dict = Depends(require_write_access),
):
    found = get_slice(slice_name)
    if not found:
        raise HTTPException(status_code=404, detail="Borrador no encontrado")
    if found.get("mode") != "graph" or found.get("state") != "draft":
        raise HTTPException(status_code=409, detail="Solo se pueden editar slices en estado draft")
    assert_can_act(user, found)
    if payload.slice_name != slice_name:
        raise HTTPException(
            status_code=409,
            detail="El nombre de un borrador existente no se cambia; usa Clonar para otro nombre",
        )

    payload = payload.model_copy(update={"vlan_base": None})
    record = _draft_record(
        payload,
        owner_username=found.get("owner_username") or user["sub"],
        owner_uid=found.get("owner_uid"),
        curso_id=found.get("curso_id"),
        created_by=found.get("created_by") or user["sub"],
        created_at=found.get("created_at"),
    )
    replace_slice(slice_name, record)
    return record


@app.post("/graph-slices/drafts/{slice_name}/deploy", status_code=202)
async def deploy_graph_slice_draft(
    slice_name: str,
    user: dict = Depends(require_write_access),
):
    found = get_slice(slice_name)
    if not found:
        raise HTTPException(status_code=404, detail="Borrador no encontrado")
    if found.get("mode") != "graph" or found.get("state") != "draft":
        raise HTTPException(status_code=409, detail="El slice no está en estado draft")
    assert_can_act(user, found)

    payload = GraphSliceCreateRequest(**_normalise_topology_from_slice(found))
    payload = _assign_vlan_for_deploy(payload, ignore_name=slice_name)
    queued = _queued_record(
        payload,
        owner_username=found.get("owner_username") or user["sub"],
        owner_uid=found.get("owner_uid"),
        curso_id=found.get("curso_id"),
        created_by=found.get("created_by") or user["sub"],
        created_at=found.get("created_at"),
    )
    replace_slice(slice_name, queued)
    try:
        job = _enqueue_create_job(
            payload,
            owner_username=queued["owner_username"],
            owner_uid=queued.get("owner_uid"),
            curso_id=queued.get("curso_id"),
            created_by=queued["created_by"],
        )
    except Exception as exc:
        replace_slice(slice_name, found)
        raise HTTPException(status_code=503, detail=f"No se pudo encolar el despliegue: {exc}") from exc

    return {"slice_name": slice_name, "job_id": job.id, "status": "queued"}


@app.get("/graph-slices/{slice_name}/export")
def export_graph_slice(slice_name: str, user: dict = Depends(current_user)):
    found = get_slice(slice_name)
    if not found or found.get("mode") != "graph":
        raise HTTPException(status_code=404, detail="Graph slice no encontrado")
    assert_can_view(user, found)
    return {
        "schema_version": "1.0",
        "kind": "pucp-private-cloud-topology",
        "exported_at": _utcnow(),
        "source_slice": slice_name,
        "topology": _normalise_topology_from_slice(found),
    }


@app.post("/graph-slices/import", status_code=201)
async def import_graph_slice(
    payload: GraphSliceImportRequest,
    user: dict = Depends(require_write_access),
):
    topology = payload.topology
    new_name = payload.new_slice_name or topology.slice_name
    topology = topology.model_copy(update={"slice_name": new_name, "vlan_base": None})
    _ensure_slice_name_available(new_name)

    owner_username, curso_id = resolve_owner_for_create(
        user, topology.owner_username, topology.curso_id
    )
    owner_uid = user["uid"] if owner_username == user["sub"] else None
    record = _draft_record(
        topology,
        owner_username=owner_username,
        owner_uid=owner_uid,
        curso_id=curso_id,
        created_by=user["sub"],
    )
    try:
        add_slice(record)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return record


@app.post("/graph-slices/{slice_name}/clone", status_code=201)
async def clone_graph_slice(
    slice_name: str,
    payload: GraphSliceCloneRequest,
    user: dict = Depends(require_write_access),
):
    found = get_slice(slice_name)
    if not found or found.get("mode") != "graph":
        raise HTTPException(status_code=404, detail="Graph slice no encontrado")
    assert_can_view(user, found)
    _ensure_slice_name_available(payload.new_slice_name)

    topology_dict = _normalise_topology_from_slice(
        found, new_name=payload.new_slice_name
    )
    topology = GraphSliceCreateRequest(**topology_dict)
    owner_username, curso_id = resolve_owner_for_create(user, None, None)
    owner_uid = user["uid"] if owner_username == user["sub"] else None
    record = _draft_record(
        topology,
        owner_username=owner_username,
        owner_uid=owner_uid,
        curso_id=curso_id,
        created_by=user["sub"],
    )
    try:
        add_slice(record)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return record


@app.post("/graph-slices", status_code=202)
async def create_graph_slice(
    payload: GraphSliceCreateRequest,
    user: dict = Depends(require_write_access),
):
    _ensure_slice_name_available(payload.slice_name)
    payload = _assign_vlan_for_deploy(payload)

    owner_username, curso_id = resolve_owner_for_create(
        user, payload.owner_username, payload.curso_id
    )
    owner_uid = user["uid"] if owner_username == user["sub"] else None
    queued = _queued_record(
        payload,
        owner_username=owner_username,
        owner_uid=owner_uid,
        curso_id=curso_id,
        created_by=user["sub"],
    )
    try:
        add_slice(queued)
        job = _enqueue_create_job(
            payload,
            owner_username=owner_username,
            owner_uid=owner_uid,
            curso_id=curso_id,
            created_by=user["sub"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        remove_slice(payload.slice_name)
        raise HTTPException(status_code=503, detail=f"No se pudo encolar el despliegue: {exc}") from exc

    return {"slice_name": payload.slice_name, "job_id": job.id, "status": "queued"}


@app.get("/graph-slices/{slice_name}/job-status")
def get_slice_job_status(slice_name: str, user: dict = Depends(current_user)):
    found = get_slice(slice_name)
    if found:
        assert_can_view(user, found)

    for prefix in ("create", "delete"):
        status = get_job_status(f"{prefix}-{slice_name}")
        if status["status"] != "not_found":
            return status

    if found:
        return {"status": found.get("state", "unknown"), "slice": found}
    return {"status": "not_found"}


@app.delete("/graph-slices/{slice_name}", status_code=202)
async def delete_graph_slice(
    slice_name: str,
    user: dict = Depends(require_write_access),
):
    found = get_slice(slice_name)
    if not found:
        raise HTTPException(status_code=404, detail="Graph slice no encontrado")
    if found.get("mode") != "graph":
        raise HTTPException(status_code=400, detail="El slice indicado no es de modo graph")
    assert_can_act(user, found)

    # Un draft no tiene infraestructura física; se elimina inmediatamente.
    if found.get("state") == "draft":
        remove_slice(slice_name)
        return {"slice_name": slice_name, "status": "deleted", "immediate": True}

    replace_slice(slice_name, {**found, "state": "deleting", "updated_at": _utcnow()})
    job = slice_queue.enqueue(
        run_delete_graph_slice_job,
        slice_name,
        found,
        job_id=f"delete-{slice_name}",
    )
    return {"slice_name": slice_name, "job_id": job.id, "status": "deleting"}


@app.post("/graph-vms/{slice_name}/{vm_name}/action")
async def action_graph_vm(
    slice_name: str,
    vm_name: str,
    payload: VMActionRequest,
    user: dict = Depends(require_write_access),
):
    found = get_slice(slice_name)
    if not found:
        raise HTTPException(status_code=404, detail="Graph slice no encontrado")
    if found.get("mode") != "graph":
        raise HTTPException(status_code=400, detail="El slice indicado no es de modo graph")
    assert_can_act(user, found)

    vm_index = next(
        (i for i, vm in enumerate(found.get("vms", [])) if vm.get("name") == vm_name),
        None,
    )
    if vm_index is None:
        raise HTTPException(status_code=404, detail="VM no encontrada en el slice")

    vm_data = {**found["vms"][vm_index], "slice_id": slice_name}
    result = await graph_orchestrator.action_graph_vm(
        vm_data, payload.action
    )
    found["vms"][vm_index]["status"] = result.get(
        "status", found["vms"][vm_index].get("status")
    )

    # Persistimos el cambio de estado SIN borrar el slice del store
    replace_slice(slice_name, found)

    return {
        "slice_name": slice_name,
        "vm_name": vm_name,
        "result": result,
    }

@app.get("/graph-vms/{slice_name}/{vm_name}/console")
async def get_vm_console(
    slice_name: str,
    vm_name: str,
    user: dict = Depends(require_token),
):
    """
    Obtiene una URL de consola VNC fresca para una VM de OpenStack.
    Los tokens de consola de Nova expiran en ~10 minutos, por lo que
    hay que pedirle uno nuevo a Nova cada vez que el usuario abre la consola.
    Para VMs Linux (QEMU), devuelve la URL del proxy WebSocket del gateway.
    """
    found = get_slice(slice_name)
    if not found:
        raise HTTPException(status_code=404, detail="Graph slice no encontrado")
    assert_can_view(user, found)

    vm = next(
        (v for v in found.get("vms", []) if v.get("name") == vm_name),
        None,
    )
    if vm is None:
        raise HTTPException(status_code=404, detail="VM no encontrada")

    # VM de OpenStack: pedir token fresco a Nova
    if vm.get("openstack_id"):
        project_id = vm.get("project_id")
        if not project_id:
            raise HTTPException(status_code=500, detail="VM sin project_id")
        try:
            from .openstack_backend.driver import OpenStackDriver
            driver = OpenStackDriver()
            scoped_token = driver.client.get_scoped_token(project_id)
            url = driver.client.get_console_url(vm["openstack_id"], scoped_token)
            if not url:
                raise HTTPException(status_code=502, detail="Nova no devolvió URL de consola")
            return {"type": "openstack", "console_url": url}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Error obteniendo consola: {exc}") from exc

    # VM Linux (QEMU): devolver info para el proxy WebSocket
    return {
        "type": "linux",
        "worker": vm.get("server") or vm.get("worker"),
        "vnc_port": vm.get("vnc_port"),
    }
