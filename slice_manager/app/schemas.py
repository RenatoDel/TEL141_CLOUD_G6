from __future__ import annotations

from pydantic import BaseModel, Field

class SliceCreateRequest(BaseModel):
    slice_name: str
    topology: str
    vlan_id: int = Field(gt=1, lt=4095)
    cidr: str
    vm_count: int = Field(ge=1)
    availability_zone: str | None = None
    has_internet: bool = False
    has_dhcp: bool = False
    dhcp_start: str = ""
    dhcp_end: str = ""
    image_name: str = "ubuntu-20.04-server-cloudimg-amd64.img"
    vnc_start: int = 5901
