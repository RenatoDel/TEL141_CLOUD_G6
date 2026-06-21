from __future__ import annotations
import os

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from .auth import require_token
from .graph_orchestrator import GraphOrchestrator
from .graph_schemas import GraphSliceCreateRequest
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

app = FastAPI(title="PUCP Slice Manager", version="0.5.0")

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
    workers = []
    for node, cluster in wanted:
        mem_total = results["mem_total"].get(node, 0.0)
        mem_avail = results["mem_avail"].get(node, 0.0)
        disk_total = results["disk_total"].get(node, 0.0)
        disk_avail = results["disk_avail"].get(node, 0.0)

        mem_used = max(mem_total - mem_avail, 0.0)
        disk_used = max(disk_total - disk_avail, 0.0)

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
# Graph slices (topologías arbitrarias)
# ════════════════════════════════════════════════════════════════════════════
@app.get("/graph-slices")
def get_graph_slices(user: dict = Depends(current_user)):
    all_graph = [s for s in list_slices() if s.get("mode") == "graph"]
    return filter_slices_for_user(user, all_graph)


@app.post("/graph-slices")
async def create_graph_slice(
    payload: GraphSliceCreateRequest,
    user: dict = Depends(require_write_access),
):
    if any(s["slice_name"] == payload.slice_name for s in list_slices()):
        raise HTTPException(status_code=409, detail="Ya existe un slice con ese nombre")

    # ─── Asignación automática de VLAN base ────────────────────────────────
    # Si el cliente no especificó vlan_base (o mandó null), calculamos el
    # siguiente libre. Si lo especificó (admin fijando una VLAN concreta),
    # lo usamos tal cual pero validamos que no esté ya en uso.
    if payload.vlan_base is None:
        links_needed = len(payload.links)
        try:
            assigned_vlan = next_free_vlan_base(links_needed)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        # Parcheamos el payload con la VLAN asignada (Pydantic v2: model_copy)
        payload = payload.model_copy(update={"vlan_base": assigned_vlan})
    else:
        # Validar que la VLAN manual no solape con slices existentes
        requested_top = payload.vlan_base + len(payload.links) - 1
        for s in list_slices():
            existing_base = s.get("vlan_base")
            if not existing_base:
                continue
            existing_links = len(s.get("links") or [])
            existing_top = existing_base + max(existing_links - 1, 0)
            if not (requested_top < existing_base or payload.vlan_base > existing_top):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"La VLAN base {payload.vlan_base} solapa con el slice "
                        f"'{s['slice_name']}' (VLANs {existing_base}–{existing_top}). "
                        "Elige otro rango o no especifiques vlan_base para asignación automática."
                    ),
                )

    owner_username, curso_id = resolve_owner_for_create(
        user, payload.owner_username, payload.curso_id
    )

    try:
        execution = await graph_orchestrator.create_graph_slice(payload)
    except RuntimeError as e:
        msg = str(e)
        if "INFEASIBLE" in msg or "Placement" in msg:
            raise HTTPException(status_code=409, detail=msg)
        raise HTTPException(status_code=500, detail=msg)
    if not execution["result"]["success"]:
        raise HTTPException(
            status_code=400,
            detail=execution["result"].get("error") or "Error creando graph slice",
        )

    stored = {
        "mode": "graph",
        "slice_name": payload.slice_name,
        "cluster": execution["cluster"],
        "network_backend": payload.network_backend,
        "internet_mode": payload.internet_mode,
        "vlan_base": payload.vlan_base,
        "workers": execution["workers"],
        "vms": execution["result"]["vms"],
        "links": execution["result"]["links"],
        "nat": execution["result"].get("nat"),
        "dhcp": execution["result"].get("dhcp", []),
        # ─── Ownership ─────────────────────────────────────────────────
        "owner_username": owner_username,
        "owner_uid": user["uid"] if owner_username == user["sub"] else None,
        "curso_id": curso_id,
        "created_by": user["sub"],
    }
    add_slice(stored)
    return execution


@app.delete("/graph-slices/{slice_name}")
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

    remove_slice(slice_name)
    result = await graph_orchestrator.delete_graph_slice(slice_name, found)
    return {"slice_name": slice_name, "result": result}


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

    result = await graph_orchestrator.action_graph_vm(
        found["vms"][vm_index], payload.action
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
