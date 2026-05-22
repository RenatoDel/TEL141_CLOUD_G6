from __future__ import annotations

from .config import settings
from .graph_schemas import GraphSliceCreateRequest
from .linux_backend.driver import LinuxDriver


class GraphOrchestrator:
    def __init__(self):
        self.driver = LinuxDriver()

    def _assign_workers(self, nodes: list[dict]) -> list[str]:
        workers = [w["name"] for w in settings.workers]
        if not workers:
            raise RuntimeError("No hay workers configurados")

        assigned = []
        rr = 0
        for node in nodes:
            preferred = node.get("preferred_worker")
            if preferred and preferred in workers:
                assigned.append(preferred)
            else:
                assigned.append(workers[rr % len(workers)])
                rr += 1
        return assigned

    async def create_graph_slice(self, payload: GraphSliceCreateRequest) -> dict:
        raw = payload.model_dump(by_alias=True)
        nodes = raw["nodes"]
        links = raw["links"]

        workers = self._assign_workers(nodes)

        enriched_nodes = []
        for idx, node in enumerate(nodes):
            enriched_nodes.append(
                {
                    **node,
                    "server": workers[idx],
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
          "workers": workers,
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
