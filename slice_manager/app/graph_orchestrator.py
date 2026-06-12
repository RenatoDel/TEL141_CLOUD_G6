from __future__ import annotations

import logging

import httpx

from .config import settings
from .graph_schemas import GraphSliceCreateRequest, GraphNodeSpec

logger = logging.getLogger("graph_orchestrator")


class GraphOrchestrator:
    def __init__(self):
        from .linux_backend.driver import LinuxDriver
        self.driver = LinuxDriver()

    async def _assign_workers(self, nodes: list[dict]) -> dict[str, str]:
        """
        Llama al placement_service con los recursos reales de cada nodo.
        Devuelve {node_name: worker_name}.

        Antes llamaba al placement solo con vm_count (round-robin en memoria).
        Ahora pasa cpu/ram_gb/disk_gb por VM para que CP-SAT optimice.
        """
        # Construir lista de VMSpec para el placement
        vms_payload = []
        for node in nodes:
            preferred = node.get("preferred_worker")
            vms_payload.append({
                "vm_id": node["name"],
                "cpu": node.get("vcpus", 1),
                "ram_gb": node.get("ram_mb", 512) / 1024.0,
                "disk_gb": node.get("disk_gb", 5),
            })

        placement_request = {
            "vms": vms_payload,
            "zone": None,       # sin filtro de zona por defecto
            "cluster": "linux",
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{settings.placement_service_url}/place",
                    json=placement_request,
                )
                response.raise_for_status()
                data = response.json()

            assignments = data["assignments"]  # {vm_id: worker_name}
            logger.info(
                "Placement CP-SAT status=%s assignments=%s",
                data.get("solver_status"),
                assignments,
            )
            return assignments

        except httpx.HTTPStatusError as exc:
            # HTTP 409: recursos insuficientes — propagar el detalle
            if exc.response.status_code == 409:
                detail = exc.response.json().get("detail", "Recursos insuficientes")
                raise RuntimeError(f"Placement INFEASIBLE: {detail}") from exc
            raise RuntimeError(
                f"Error del placement_service: {exc.response.status_code}"
            ) from exc
        except Exception as exc:
            # Fallback: si el placement_service no está disponible, usar round-robin local
            logger.warning(
                "placement_service no disponible (%s), usando round-robin local como fallback",
                exc,
            )
            return self._round_robin_fallback(nodes)

    def _round_robin_fallback(self, nodes: list[dict]) -> dict[str, str]:
        """
        Round-robin local. Solo se usa si el placement_service no responde.
        Respeta preferred_worker si se especifica.
        """
        workers = [w["name"] for w in settings.workers]
        if not workers:
            raise RuntimeError("No hay workers configurados")

        assignments: dict[str, str] = {}
        rr = 0
        for node in nodes:
            preferred = node.get("preferred_worker")
            if preferred and preferred in workers:
                assignments[node["name"]] = preferred
            else:
                assignments[node["name"]] = workers[rr % len(workers)]
                rr += 1
        return assignments

    async def create_graph_slice(self, payload: GraphSliceCreateRequest) -> dict:
        raw = payload.model_dump(by_alias=True)
        nodes = raw["nodes"]
        links = raw["links"]

        # Obtener asignación óptima del placement_service
        assignments = await self._assign_workers(nodes)

        enriched_nodes = []
        for idx, node in enumerate(nodes):
            worker_name = assignments.get(node["name"])
            if not worker_name:
                raise RuntimeError(
                    f"El placement no devolvió asignación para el nodo {node['name']!r}"
                )
            enriched_nodes.append(
                {
                    **node,
                    "server": worker_name,
                    "vnc_port": payload.vnc_start + idx,
                }
            )

        request = {
            "slice_id": payload.slice_name,
            "nodes": enriched_nodes,
            "links": links,
            "vlan_base": payload.vlan_base,
            "network_backend": payload.network_backend,
            "internet_mode": payload.internet_mode,
        }

        result = self.driver.create_graph_slice(request)

        return {
            "slice_name": payload.slice_name,
            "network_backend": payload.network_backend,
            "internet_mode": payload.internet_mode,
            "workers": assignments,
            "result": result,
        }

    async def delete_graph_slice(self, slice_name: str, found: dict) -> dict:
        return self.driver.delete_graph_slice(
            slice_id=slice_name,
            vms=found.get("vms", []),
            nat=found.get("nat"),
            dhcp=found.get("dhcp", []),
        )

    async def action_graph_vm(self, vm: dict, action: str) -> dict:
        return self.driver.action_graph_vm(vm, action)