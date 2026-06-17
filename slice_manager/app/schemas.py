from __future__ import annotations

from typing import Optional

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

    # ─── Ownership / curso (opcionales, controlados por RBAC) ─────────────
    # Si se omiten, el dueño es el caller y curso_id queda en NULL.
    # Solo admin/profesor pueden setear owner_username distinto al caller.
    # Solo profesor puede setear curso_id (debe ser un curso que dicta).
    owner_username: Optional[str] = None
    curso_id: Optional[int] = None
