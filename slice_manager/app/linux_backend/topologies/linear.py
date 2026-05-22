from __future__ import annotations

from dataclasses import dataclass

@dataclass
class LinearVM:
    vm_id: str
    name: str
    server: str
    vnc_port: int
    position: int

@dataclass
class LinearSlice:
    slice_id: str
    vlan_id: int
    cidr: str
    vms: list[LinearVM]
    has_internet: bool = False
    has_dhcp: bool = False
    dhcp_start: str = ""
    dhcp_end: str = ""

def build_linear_slice(slice_id: str, vlan_id: int, cidr: str, vm_count: int, servers: list[str],
                       vnc_start: int = 5901, has_internet: bool = False, has_dhcp: bool = False,
                       dhcp_start: str = "", dhcp_end: str = "") -> LinearSlice:
    if len(servers) != vm_count:
        raise ValueError("Debe haber un servidor por VM")
    vms = [LinearVM(f"{slice_id}-vm{i+1}", f"{slice_id}-vm{i+1}", servers[i], vnc_start + i, i) for i in range(vm_count)]
    return LinearSlice(slice_id, vlan_id, cidr, vms, has_internet, has_dhcp, dhcp_start, dhcp_end)

def get_linear_summary(slice_obj: LinearSlice) -> dict:
    return {
        "topology": "linear",
        "slice_id": slice_obj.slice_id,
        "vm_count": len(slice_obj.vms),
        "links": [{"from": slice_obj.vms[i].vm_id, "to": slice_obj.vms[i+1].vm_id} for i in range(len(slice_obj.vms)-1)],
    }
