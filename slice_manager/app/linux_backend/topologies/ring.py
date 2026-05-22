from __future__ import annotations

from dataclasses import dataclass

@dataclass
class RingVM:
    vm_id: str
    name: str
    server: str
    vnc_port: int
    position: int

@dataclass
class RingSlice:
    slice_id: str
    vlan_id: int
    cidr: str
    vms: list[RingVM]
    has_internet: bool = False
    has_dhcp: bool = False
    dhcp_start: str = ""
    dhcp_end: str = ""

def build_ring_slice(slice_id: str, vlan_id: int, cidr: str, vm_count: int, servers: list[str],
                     vnc_start: int = 5901, has_internet: bool = False, has_dhcp: bool = False,
                     dhcp_start: str = "", dhcp_end: str = "") -> RingSlice:
    if vm_count < 3:
        raise ValueError("Ring requiere mínimo 3 VMs")
    if len(servers) != vm_count:
        raise ValueError("Debe haber un servidor por VM")
    vms = [RingVM(f"{slice_id}-vm{i+1}", f"{slice_id}-vm{i+1}", servers[i], vnc_start + i, i) for i in range(vm_count)]
    return RingSlice(slice_id, vlan_id, cidr, vms, has_internet, has_dhcp, dhcp_start, dhcp_end)

def get_ring_summary(slice_obj: RingSlice) -> dict:
    n = len(slice_obj.vms)
    return {
        "topology": "ring",
        "slice_id": slice_obj.slice_id,
        "vm_count": n,
        "links": [{"from": slice_obj.vms[i].vm_id, "to": slice_obj.vms[(i+1) % n].vm_id} for i in range(n)],
    }
