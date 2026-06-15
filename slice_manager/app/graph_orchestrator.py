from __future__ import annotations

import logging

import httpx

from .config import settings
from .graph_schemas import GraphSliceCreateRequest, GraphNodeSpec

logger = logging.getLogger("graph_orchestrator")

# Cluster que se considera OpenStack
OPENSTACK_ZONES = {"az-openstack", "openstack"}
OPENSTACK_CLUSTER = "openstack"


def _get_driver(cluster: str):
    """
    Devuelve el driver correcto según el cluster solicitado.
    cluster = "linux"      → LinuxDriver
    cluster = "openstack"  → OpenStackDriver
    """
    if cluster == OPENSTACK_CLUSTER:
        from .openstack_backend.driver import OpenStackDriver
        return OpenStackDriver()
    else:
        from .linux_backend.driver import LinuxDriver
        return LinuxDriver()


def _zone_to_cluster(zone: str | None) -> str:
    """Infiere el cluster a partir de la zona de disponibilidad."""
    if zone and zone.lower() in OPENSTACK_ZONES:
        return OPENSTACK_CLUSTER
    return "linux"


class GraphOrchestrator:
    def __init__(self):
        # Driver por defecto Linux — se reemplaza en create_graph_slice
        # según el cluster pedido por el usuario
        from .linux_backend.driver import LinuxDriver
        self.driver = LinuxDriver()
        self._current_cluster = "linux"

    def _set_driver(self, cluster: str):
        """Instancia el driver correcto si cambió el cluster."""
        if cluster != self._current_cluster:
            self.driver = _get_driver(cluster)
            self._current_cluster = cluster
            logger.info("Driver cambiado a: %s", cluster)

    async def _assign_workers(
        self,
        nodes: list[dict],
        zone: str | None = None,
        cluster: str = "linux",
    ) -> dict[str, str]:
        """
        Llama al placement_service con los recursos reales de cada nodo.
        Devuelve {node_name: worker_name}.
        """
        vms_payload = []
        for node in nodes:
            vms_payload.append({
                "vm_id": node["name"],
                "cpu": node.get("vcpus", 1),
                "ram_gb": node.get("ram_mb", 512) / 1024.0,
                "disk_gb": node.get("disk_gb", 5),
            })

        placement_request = {
            "vms": vms_payload,
            "zone": zone,
            "cluster": cluster,
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
                "Placement CP-SAT status=%s cluster=%s assignments=%s",
                data.get("solver_status"),
                cluster,
                assignments,
            )
            return assignments

        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                detail = exc.response.json().get("detail", "Recursos insuficientes")
                raise RuntimeError(f"Placement INFEASIBLE: {detail}") from exc
            raise RuntimeError(
                f"Error del placement_service: {exc.response.status_code}"
            ) from exc
        except Exception as exc:
            logger.warning(
                "placement_service no disponible (%s), usando round-robin local como fallback",
                exc,
            )
            return self._round_robin_fallback(nodes, cluster)

    def _round_robin_fallback(
        self, nodes: list[dict], cluster: str = "linux"
    ) -> dict[str, str]:
        """
        Round-robin local. Solo se usa si el placement_service no responde.
        Para OpenStack usa workers de OpenStack si están configurados.
        """
        if cluster == OPENSTACK_CLUSTER:
            # Fallback para OpenStack: distribuir sin worker específico
            # El driver OpenStack ignora el worker si no puede hacer pin de AZ
            return {node["name"]: f"os-worker{(i % 3) + 1}" for i, node in enumerate(nodes)}

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

        # Determinar cluster según la zona solicitada
        zone = getattr(payload, "availability_zone", None)
        cluster = getattr(payload, "cluster", None)
        if not cluster:
            cluster = _zone_to_cluster(zone)

        logger.info(
            "create_graph_slice: slice=%s cluster=%s zone=%s",
            payload.slice_name, cluster, zone,
        )

        # Seleccionar driver correcto
        self._set_driver(cluster)

        # Si es OpenStack y no se especificó zona, usar az-openstack por defecto
        if cluster == OPENSTACK_CLUSTER and not zone:
            zone = "az-openstack"
        # Obtener asignación óptima del placement_service
        assignments = await self._assign_workers(nodes, zone=zone, cluster=cluster)

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
            "cluster": cluster,
        }

        result = self.driver.create_graph_slice(request)

        return {
            "slice_name": payload.slice_name,
            "cluster": cluster,
            "network_backend": payload.network_backend,
            "internet_mode": payload.internet_mode,
            "workers": assignments,
            "result": result,
        }

    async def delete_graph_slice(self, slice_name: str, found: dict) -> dict:
        # Detectar cluster del slice guardado
        cluster = found.get("cluster", "linux")
        self._set_driver(cluster)

        return self.driver.delete_graph_slice(
            slice_id=slice_name,
            vms=found.get("vms", []),
            nat=found.get("nat"),
            dhcp=found.get("dhcp", []),
        )

    async def action_graph_vm(self, vm: dict, action: str) -> dict:
        # Detectar cluster de la VM
        cluster = vm.get("cluster", "linux")
        if vm.get("openstack_id"):
            cluster = OPENSTACK_CLUSTER
        self._set_driver(cluster)

        return self.driver.action_graph_vm(vm, action)