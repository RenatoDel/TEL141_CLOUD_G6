from __future__ import annotations
import os

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from .auth import require_token
from .graph_orchestrator import GraphOrchestrator
from .graph_schemas import GraphSliceCreateRequest
from .orchestrator import Orchestrator
from .schemas import SliceCreateRequest
from .state_store import add_slice, delete_slice as remove_slice, list_slices

app = FastAPI(title="PUCP Slice Manager", version="0.4.0")

orchestrator = Orchestrator()
graph_orchestrator = GraphOrchestrator()


class VMActionRequest(BaseModel):
    action: str


@app.get("/health")
def health():
    return {"status": "ok"}


### BEGIN MONITORING_SUMMARY ###
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
async def monitoring_summary(user=Depends(require_token)):
    queries = {
        "up": 'up{job=~"node_exporter_.*"}',
        "cpu": '100 * (1 - avg by (node) (rate(node_cpu_seconds_total{mode="idle"}[5m])))',
        "mem_total": 'node_memory_MemTotal_bytes',
        "mem_avail": 'node_memory_MemAvailable_bytes',
        "disk_total": 'node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay|squashfs"}',
        "disk_avail": 'node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay|squashfs"}',
    }

    results = {}
    for key, expr in queries.items():
        try:
            results[key] = _vector_to_node_map(await _prom_query(expr))
        except Exception:
            results[key] = {}

    wanted = ["server1", "server2", "server3", "server4-headnode"]
    workers = []
    for node in wanted:
        mem_total = results["mem_total"].get(node, 0.0)
        mem_avail = results["mem_avail"].get(node, 0.0)
        disk_total = results["disk_total"].get(node, 0.0)
        disk_avail = results["disk_avail"].get(node, 0.0)

        mem_used = max(mem_total - mem_avail, 0.0)
        disk_used = max(disk_total - disk_avail, 0.0)

        workers.append({
            "worker": node,
            "status": "up" if results["up"].get(node, 0.0) >= 1 else "down",
            "cpu_percent": round(results["cpu"].get(node, 0.0), 2),
            "mem_total_gb": round(mem_total / (1024**3), 2),
            "mem_used_gb": round(mem_used / (1024**3), 2),
            "mem_free_gb": round(mem_avail / (1024**3), 2),
            "disk_total_gb": round(disk_total / (1024**3), 2),
            "disk_used_gb": round(disk_used / (1024**3), 2),
            "disk_free_gb": round(disk_avail / (1024**3), 2),
        })

    totals = {
        "workers_total": len(workers),
        "workers_up": sum(1 for w in workers if w["status"] == "up"),
        "mem_total_gb": round(sum(w["mem_total_gb"] for w in workers), 2),
        "mem_used_gb": round(sum(w["mem_used_gb"] for w in workers), 2),
        "disk_total_gb": round(sum(w["disk_total_gb"] for w in workers), 2),
        "disk_used_gb": round(sum(w["disk_used_gb"] for w in workers), 2),
        "avg_cpu_percent": round(
            sum(w["cpu_percent"] for w in workers if w["status"] == "up") /
            max(sum(1 for w in workers if w["status"] == "up"), 1), 2
        ),
    }

    return {"workers": workers, "totals": totals}
### END MONITORING_SUMMARY ###



@app.get("/slices")
def get_slices(user=Depends(require_token)):
    return list_slices()


@app.post("/slices")
async def create_slice(payload: SliceCreateRequest, user=Depends(require_token)):
    if payload.topology not in {"linear", "ring"}:
        raise HTTPException(status_code=400, detail="Topología no soportada por ahora")

    if payload.topology == "ring" and payload.vm_count < 3:
        raise HTTPException(status_code=400, detail="Ring requiere mínimo 3 VMs")

    existing = list_slices()
    if any(s["slice_name"] == payload.slice_name for s in existing):
        raise HTTPException(status_code=409, detail="Ya existe un slice con ese nombre")

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
    }

    add_slice(stored)
    return execution


@app.delete("/slices/{slice_name}")
async def delete_slice(slice_name: str, user=Depends(require_token)):
    found = remove_slice(slice_name)
    if not found:
        raise HTTPException(status_code=404, detail="Slice no encontrado")

    result = await orchestrator.delete_slice(slice_name, found)
    return {"slice_name": slice_name, "result": result}


@app.get("/graph-slices")
def get_graph_slices(user=Depends(require_token)):
    return [s for s in list_slices() if s.get("mode") == "graph"]


@app.post("/graph-slices")
async def create_graph_slice(payload: GraphSliceCreateRequest, user=Depends(require_token)):
    existing = list_slices()
    if any(s["slice_name"] == payload.slice_name for s in existing):
        raise HTTPException(status_code=409, detail="Ya existe un slice con ese nombre")

    execution = await graph_orchestrator.create_graph_slice(payload)

    if not execution["result"]["success"]:
        raise HTTPException(
            status_code=400,
            detail=execution["result"].get("error") or "Error creando graph slice",
        )

    stored = {
        "mode": "graph",
        "slice_name": payload.slice_name,
        "network_backend": payload.network_backend,
        "internet_mode": payload.internet_mode,
        "vlan_base": payload.vlan_base,
        "workers": execution["workers"],
        "vms": execution["result"]["vms"],
        "links": execution["result"]["links"],
        "nat": execution["result"].get("nat"),
        "dhcp": execution["result"].get("dhcp", []),
    }

    add_slice(stored)
    return execution


@app.delete("/graph-slices/{slice_name}")
async def delete_graph_slice(slice_name: str, user=Depends(require_token)):
    found = remove_slice(slice_name)
    if not found:
        raise HTTPException(status_code=404, detail="Graph slice no encontrado")

    if found.get("mode") != "graph":
        raise HTTPException(status_code=400, detail="El slice indicado no es de modo graph")

    result = await graph_orchestrator.delete_graph_slice(slice_name, found)
    return {"slice_name": slice_name, "result": result}


@app.post("/graph-vms/{slice_name}/{vm_name}/action")
async def action_graph_vm(slice_name: str, vm_name: str, payload: VMActionRequest, user=Depends(require_token)):
    found = remove_slice(slice_name)
    if not found:
        raise HTTPException(status_code=404, detail="Graph slice no encontrado")

    try:
        if found.get("mode") != "graph":
            raise HTTPException(status_code=400, detail="El slice indicado no es de modo graph")

        vm_index = next((i for i, vm in enumerate(found.get("vms", [])) if vm.get("name") == vm_name), None)
        if vm_index is None:
            raise HTTPException(status_code=404, detail="VM no encontrada en el slice")

        result = await graph_orchestrator.action_graph_vm(found["vms"][vm_index], payload.action)
        found["vms"][vm_index]["status"] = result.get("status", found["vms"][vm_index].get("status"))

        return {
            "slice_name": slice_name,
            "vm_name": vm_name,
            "result": result,
        }
    finally:
        add_slice(found)
