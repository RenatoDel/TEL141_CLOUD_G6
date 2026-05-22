from __future__ import annotations

import httpx
from .config import settings
from .linux_backend.driver import LinuxDriver, SliceRequest

class Orchestrator:
    def __init__(self) -> None:
        self.driver = LinuxDriver()

    async def plan_workers(self, vm_count: int, availability_zone: str | None) -> list[str]:
        payload = {"vm_count": vm_count, "availability_zone": availability_zone}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{settings.placement_service_url}/place", json=payload)
            response.raise_for_status()
            data = response.json()
        return data["workers"]

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
        servers = await self.plan_workers(payload.vm_count, payload.availability_zone)
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
