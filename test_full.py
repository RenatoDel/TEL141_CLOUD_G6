from __future__ import annotations
import logging
logging.basicConfig(level=logging.INFO, format='%(message)s')

from driver import LinuxDriver, SliceRequest

driver = LinuxDriver(ssh_mode="internal")

print("\n=== TEST 1: Slice lineal 2 VMs en server1 ===")
req = SliceRequest(
    slice_id  = "test-linear-001",
    topology  = "linear",
    vlan_id   = 101,
    cidr      = "192.168.101.0/24",
    vm_count  = 2,
    servers   = ["server1", "server1"],
    vnc_start = 5911,
)
result = driver.create_slice(req)
print(f"Resultado: {'OK' if result.success else 'FALLO'}")
for vm in result.vms:
    print(f"  {vm.name}: {vm.status} en {vm.server} VNC:{vm.vnc_port}")

if result.success:
    print("\nBorrando slice lineal...")
    vms = [{"name": v.name, "server": v.server, "vm_id": v.vm_id, "vnc_port": v.vnc_port} for v in result.vms]
    ok = driver.delete_slice("test-linear-001", 101, "192.168.101.0/24", vms)
    print(f"Borrado: {'OK' if ok else 'FALLO'}")

print("\n=== TEST 2: Slice anillo 3 VMs en server1 y server2 ===")
req2 = SliceRequest(
    slice_id  = "test-ring-001",
    topology  = "ring",
    vlan_id   = 201,
    cidr      = "192.168.201.0/24",
    vm_count  = 3,
    servers   = ["server1", "server2", "server1"],
    vnc_start = 5921,
)
result2 = driver.create_slice(req2)
print(f"Resultado: {'OK' if result2.success else 'FALLO'}")
for vm in result2.vms:
    print(f"  {vm.name}: {vm.status} en {vm.server} VNC:{vm.vnc_port}")

if result2.success:
    print("\nBorrando slice anillo...")
    vms2 = [{"name": v.name, "server": v.server, "vm_id": v.vm_id, "vnc_port": v.vnc_port} for v in result2.vms]
    ok2 = driver.delete_slice("test-ring-001", 201, "192.168.201.0/24", vms2)
    print(f"Borrado: {'OK' if ok2 else 'FALLO'}")

print("\n=== Pruebas completadas ===")
