from __future__ import annotations

import logging

import httpx

from .config import settings
from .linux_backend.driver import LinuxDriver, SliceRequest

logger = logging.getLogger("orchestrator")


class Orchestrator:
    def __init__(self) -> None:
        self.driver = LinuxDriver()

    async def plan_workers(
        self,
        vm_count: int,
        availability_zone: str | None,
        vm_specs: list[dict] | None = None,
    ) -> list[str]:
        """
        Llama al placement_service para asignar workers a las VMs.

        vm_specs: lista de dicts con {vm_id, cpu, ram_gb, disk_gb}.
        Si no se provee (llamadas legacy), genera specs uniformes con defaults.
        Devuelve lista ordenada de worker names (mismo orden que vm_specs).
        """
        if vm_specs is None:
            # Compatibilidad con llamadas anteriores que solo pasaban vm_count
            vm_specs = [
                {"vm_id": f"vm{i+1}", "cpu": 1, "ram_gb": 0.5, "disk_gb": 5}
                for i in range(vm_count)
            ]

        placement_request = {
            "vms": vm_specs,
            "zone": availability_zone,
            "cluster": "linux",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{settings.placement_service_url}/place",
                json=placement_request,
            )
            response.raise_for_status()
            data = response.json()

        assignments = data["assignments"]  # {vm_id: worker_name}
        logger.info(
            "Placement status=%s workers=%s",
            data.get("solver_status"),
            assignments,
        )

        # Devolver lista en el mismo orden que vm_specs
        return [assignments[spec["vm_id"]] for spec in vm_specs]

    async def validate_image(self, image_name: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{settings.image_service_url}/images")
            response.raise_for_status()
            images = response.json()
        image = next((img for img in images if img["name"] == image_name), None)
        if not image:
            raise ValueError(f"Imagen no registrada: {image_name}")
        return image

    async def create_slice(self, payload) -> dict:
        # Construir vm_specs con los recursos reales del payload si están disponibles
        vm_specs = None
        if hasattr(payload, "vm_specs") and payload.vm_specs:
            vm_specs = [
                {
                    "vm_id": f"vm{i+1}",
                    "cpu": spec.vcpus,
                    "ram_gb": spec.ram_mb / 1024.0,
                    "disk_gb": spec.disk_gb,
                }
                for i, spec in enumerate(payload.vm_specs)
            ]

        servers = await self.plan_workers(
            payload.vm_count,
            payload.availability_zone,
            vm_specs=vm_specs,
        )
        image = await self.validate_image(payload.image_name)

        request = SliceRequest(
            slice_id=payload.slice_name,
            topology=payload.topology,
            vlan_id=payload.vlan_id,
            cidr=payload.cidr,
            vm_count=payload.vm_count,
            servers=servers,
            vnc_start=payload.vnc_start,
            has_internet=payload.has_internet,
            has_dhcp=payload.has_dhcp,
            dhcp_start=payload.dhcp_start,
            dhcp_end=payload.dhcp_end,
            image_name=image["filename"],
        )
        result = self.driver.create_slice(request)
        return {
            "slice_name": payload.slice_name,
            "topology": payload.topology,
            "deploy_mode": settings.deploy_mode,
            "workers": servers,
            "image": image,
            "result": result,
        }

    async def delete_slice(self, slice_name: str, slice_data: dict) -> dict:
        return self.driver.delete_slice(
            slice_id=slice_name,
            vlan_id=slice_data["vlan_id"],
            cidr=slice_data["cidr"],
            vms=slice_data["vms"],
        )