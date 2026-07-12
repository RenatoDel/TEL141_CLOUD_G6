from __future__ import annotations

import hashlib
import ipaddress
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx

from ..config import settings
from .network_manager import NetworkManager
from .ssh_client import SSHClient
from .topologies import SUPPORTED, build_linear_slice, build_ring_slice
from .vm_manager import VMConfig, VMInterface, VMManager

logger = logging.getLogger(__name__)
OVS_BRIDGE = "br-int"


@dataclass
class SliceRequest:
    slice_id: str
    topology: str
    vlan_id: int
    cidr: str
    vm_count: int
    servers: list[str]
    vnc_start: int = 5901
    has_internet: bool = False
    has_dhcp: bool = False
    dhcp_start: str = ""
    dhcp_end: str = ""
    image_name: str = "cirros-base.img"


@dataclass
class VMResult:
    vm_id: str
    name: str
    server: str
    vnc_port: int
    status: str
    error: Optional[str] = None
    interfaces: list[dict] = field(default_factory=list)
    image_name: Optional[str] = None
    vcpus: Optional[int] = None
    ram_mb: Optional[int] = None
    disk_gb: Optional[int] = None
    stored_filename: Optional[str] = None
    image_sync: Optional[dict] = None


@dataclass
class SliceResult:
    slice_id: str
    topology: str
    success: bool
    vms: list[VMResult] = field(default_factory=list)
    error: Optional[str] = None


class LinuxDriver:
    def _worker_map(self) -> dict[str, dict]:
        return {w["name"]: w for w in settings.workers}

    def _headnode_client(self) -> SSHClient:
        return SSHClient(
            settings.headnode_ssh_host,
            settings.headnode_ssh_user,
            settings.headnode_ssh_port,
            settings.headnode_ssh_key_path,
        )

    def _worker_client(self, worker_name: str) -> SSHClient:
        worker = self._worker_map()[worker_name]
        return SSHClient(
            worker["host"],
            settings.headnode_ssh_user,
            worker.get("port", 22),
            settings.headnode_ssh_key_path,
        )

    def _stable_mac(self, label: str) -> str:
        h = hashlib.md5(label.encode()).digest()
        return "02:%02x:%02x:%02x:%02x:%02x" % tuple(h[:5])

    def _local_sha256(self, path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _node_is_cirros(self, node: dict) -> bool:
        return "cirros" in (node.get("image_name") or "").lower()

    def _image_record_by_name(self, image_name: str) -> dict:
        url = f"{settings.image_service_url}/images/by-name/{quote(image_name, safe='')}"
        response = httpx.get(url, timeout=60.0)
        response.raise_for_status()
        return response.json()

    def _download_image_to_cache(self, record: dict) -> Path:
        cache_dir = Path(settings.image_sync_cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)

        local_path = cache_dir / record["stored_filename"]
        if local_path.exists():
            if local_path.stat().st_size == record["size_bytes"] and self._local_sha256(local_path) == record["sha256"]:
                return local_path
            local_path.unlink(missing_ok=True)

        url = f"{settings.image_service_url}/images/download/{quote(record['stored_filename'], safe='')}"
        tmp_path = local_path.with_suffix(local_path.suffix + ".part")

        with httpx.stream("GET", url, timeout=None) as response:
            response.raise_for_status()
            with tmp_path.open("wb") as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)

        if tmp_path.stat().st_size != record["size_bytes"]:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError("size_bytes no coincide tras la descarga")

        if self._local_sha256(tmp_path) != record["sha256"]:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError("sha256 no coincide tras la descarga")

        tmp_path.replace(local_path)
        return local_path

    def _remote_size(self, ssh: SSHClient, remote_path: str) -> int | None:
        out, _ = ssh.sudo(f"stat -c%s {remote_path}", raise_on_error=False)
        text = out.strip()
        if not text.isdigit():
            return None
        return int(text)

    def _remote_sha256(self, ssh: SSHClient, remote_path: str) -> str | None:
        out, _ = ssh.sudo(f"sha256sum {remote_path}", raise_on_error=False)
        text = out.strip()
        if not text:
            return None
        return text.split()[0]

    def _remote_file_matches(self, ssh: SSHClient, remote_path: str, record: dict) -> bool:
        if not ssh.file_exists(remote_path):
            return False
        size = self._remote_size(ssh, remote_path)
        if size != record["size_bytes"]:
            return False
        sha = self._remote_sha256(ssh, remote_path)
        return sha == record["sha256"]

    def _ensure_remote_image(self, ssh: SSHClient, record: dict, remote_dir: str) -> dict:
        remote_path = f"{remote_dir.rstrip('/')}/{record['stored_filename']}"
        if self._remote_file_matches(ssh, remote_path, record):
            return {"status": "cached", "path": remote_path, "stored_filename": record["stored_filename"]}

        local_path = self._download_image_to_cache(record)
        ssh.upload_file(str(local_path), remote_path, mode=0o644)

        if not self._remote_file_matches(ssh, remote_path, record):
            raise RuntimeError(f"No se pudo verificar la imagen sincronizada en {ssh.host}: {remote_path}")

        return {"status": "uploaded", "path": remote_path, "stored_filename": record["stored_filename"]}

    def _sync_image_for_targets(self, image_name: str, target_workers: list[str]) -> dict:
        record = self._image_record_by_name(image_name)

        with self._headnode_client() as headnode:
            headnode_state = self._ensure_remote_image(headnode, record, settings.headnode_image_dir)

        workers_state = {}
        for worker_name in sorted(set(target_workers)):
            with self._worker_client(worker_name) as worker:
                workers_state[worker_name] = self._ensure_remote_image(worker, record, settings.worker_image_dir)

        return {"record": record, "headnode": headnode_state, "workers": workers_state}

    def _sync_graph_images(self, nodes: list[dict]) -> list[dict]:
        image_targets: dict[str, set[str]] = {}
        for node in nodes:
            image_targets.setdefault(node["image_name"], set()).add(node["server"])

        sync_results = {
            image_name: self._sync_image_for_targets(image_name, sorted(targets))
            for image_name, targets in image_targets.items()
        }

        synced_nodes = []
        for node in nodes:
            sync = sync_results[node["image_name"]]
            synced_nodes.append(
                {
                    **node,
                    "resolved_image_name": sync["record"]["stored_filename"],
                    "image_sync": {
                        "headnode": sync["headnode"]["status"],
                        "worker": sync["workers"][node["server"]]["status"],
                    },
                }
            )
        return synced_nodes

    def _sync_legacy_image(self, image_name: str, servers: list[str]) -> tuple[dict, str]:
        sync = self._sync_image_for_targets(image_name, sorted(set(servers)))
        return sync, sync["record"]["stored_filename"]

    def _graph_vm_payload(
        self,
        *,
        node: dict,
        status: str,
        error: str | None,
        interfaces: list[dict],
        ssh_forward: dict | None = None,
    ) -> dict:
        payload = {
            "vm_id": node["name"],
            "name": node["name"],
            "server": node["server"],
            "vnc_port": node["vnc_port"],
            "status": status,
            "error": error,
            "interfaces": interfaces,
            "loopback_cidr": node.get("loopback_cidr"),
            "image_name": node.get("image_name"),
            "vcpus": node.get("vcpus"),
            "ram_mb": node.get("ram_mb"),
            "disk_gb": node.get("disk_gb"),
            "internet": node.get("internet", False),
            "preferred_worker": node.get("preferred_worker"),
            "stored_filename": node.get("resolved_image_name"),
            "image_sync": node.get("image_sync"),
        }

        if ssh_forward:
            payload.update(
                {
                    "ssh_host": ssh_forward.get("public_host") or ssh_forward.get("headnode_ip"),
                    "ssh_port": ssh_forward.get("ssh_port"),
                    "ssh_user": ssh_forward.get("ssh_user"),
                    "ssh_command": ssh_forward.get("ssh_command"),
                    "ssh_target_ip": ssh_forward.get("target_ip"),
                    "ssh_target_port": ssh_forward.get("target_port", 22),
                    "ssh_credential_hint": ssh_forward.get("credential_hint"),
                    "external_ip": ssh_forward.get("external_endpoint"),
                }
            )

        if ssh_forward:
            payload["ssh_access_mode"] = (
                ssh_forward.get("access_mode")
                or ssh_forward.get("ssh_access_mode")
            )
            payload["ssh_jump_user"] = (
                ssh_forward.get("jump_user")
                or ssh_forward.get("ssh_jump_user")
            )
            payload["ssh_jump_host"] = (
                ssh_forward.get("jump_host")
                or ssh_forward.get("ssh_jump_host")
                or ssh_forward.get("public_host")
            )
            payload["ssh_jump_port"] = (
                ssh_forward.get("jump_port")
                or ssh_forward.get("ssh_jump_port")
            )
            if payload.get("ssh_access_mode") == "proxyjump" and payload.get("ssh_jump_port"):
                payload["ssh_port"] = payload["ssh_jump_port"]

        return payload

    # =========================
    # LEGACY MODE
    # =========================

    def create_slice(self, request: SliceRequest) -> dict:
        if request.topology not in SUPPORTED:
            return asdict(
                SliceResult(
                    request.slice_id,
                    request.topology,
                    False,
                    error=f"Topología no soportada: {request.topology}",
                )
            )

        if request.topology == "linear":
            slice_obj = build_linear_slice(
                request.slice_id,
                request.vlan_id,
                request.cidr,
                request.vm_count,
                request.servers,
                request.vnc_start,
                request.has_internet,
                request.has_dhcp,
                request.dhcp_start,
                request.dhcp_end,
            )
        else:
            slice_obj = build_ring_slice(
                request.slice_id,
                request.vlan_id,
                request.cidr,
                request.vm_count,
                request.servers,
                request.vnc_start,
                request.has_internet,
                request.has_dhcp,
                request.dhcp_start,
                request.dhcp_end,
            )

        resolved_image_name = request.image_name
        image_sync = None

        if settings.deploy_mode != "dry_run":
            image_sync, resolved_image_name = self._sync_legacy_image(request.image_name, request.servers)

        if settings.deploy_mode == "dry_run":
            vms = [
                VMResult(
                    vm.vm_id,
                    vm.name,
                    vm.server,
                    vm.vnc_port,
                    "planned",
                    image_name=request.image_name,
                    vcpus=1,
                    ram_mb=256,
                    stored_filename=resolved_image_name,
                )
                for vm in slice_obj.vms
            ]
            return asdict(SliceResult(request.slice_id, request.topology, True, vms=vms))

        created_vms = []
        vm_results: list[VMResult] = []

        try:
            with self._headnode_client() as headnode:
                net_mgr = NetworkManager(headnode)
                net_mgr.create_vlan_network(
                    request.vlan_id,
                    request.cidr,
                    request.has_dhcp,
                    request.dhcp_start or None,
                    request.dhcp_end or None,
                )
                if request.has_internet:
                    net_mgr.enable_internet(request.vlan_id, request.cidr)

            for vm in slice_obj.vms:
                try:
                    with self._worker_client(vm.server) as client:
                        mgr = VMManager(client)
                        cfg = VMConfig(
                            name=vm.name,
                            ovs_bridge=OVS_BRIDGE,
                            vlan_id=request.vlan_id,
                            vnc_port=vm.vnc_port,
                            base_image=resolved_image_name,
                        )
                        mgr.create_vm(cfg)
                        time.sleep(3)
                        status = mgr.get_vm_status(vm.name)
                        if status != "running":
                            raise RuntimeError(f"La VM {vm.name} no quedó running; estado={status}")

                    result = VMResult(
                        vm.vm_id,
                        vm.name,
                        vm.server,
                        vm.vnc_port,
                        status,
                        image_name=request.image_name,
                        vcpus=1,
                        ram_mb=256,
                        stored_filename=resolved_image_name,
                        image_sync={
                            "headnode": image_sync["headnode"]["status"],
                            "worker": image_sync["workers"][vm.server]["status"],
                        },
                    )
                    created_vms.append(vm)
                    vm_results.append(result)

                except Exception as exc:
                    logger.exception("Error creando VM %s", vm.name)
                    vm_results.append(
                        VMResult(
                            vm.vm_id,
                            vm.name,
                            vm.server,
                            vm.vnc_port,
                            "error",
                            str(exc),
                            image_name=request.image_name,
                            vcpus=1,
                            ram_mb=256,
                            stored_filename=resolved_image_name,
                        )
                    )
                    raise

            return asdict(SliceResult(request.slice_id, request.topology, True, vm_results))

        except Exception as exc:
            logger.exception("Fallo creando slice %s", request.slice_id)
            self._rollback(request, created_vms)
            return asdict(SliceResult(request.slice_id, request.topology, False, vm_results, str(exc)))

    def delete_slice(self, slice_id: str, vlan_id: int, cidr: str, vms: list[dict]) -> dict:
        if settings.deploy_mode == "dry_run":
            return {"slice_id": slice_id, "success": True, "mode": "dry_run"}

        success = True

        for vm in vms:
            try:
                with self._worker_client(vm["server"]) as client:
                    VMManager(client).delete_vm(vm["name"], OVS_BRIDGE)
            except Exception as exc:
                logger.error("Error borrando VM %s: %s", vm["name"], exc)
                success = False

        try:
            with self._headnode_client() as headnode:
                NetworkManager(headnode).delete_vlan_network(vlan_id, cidr)
        except Exception as exc:
            logger.error("Error borrando red VLAN %s: %s", vlan_id, exc)
            success = False

        return {"slice_id": slice_id, "success": success}

    def _rollback(self, request: SliceRequest, created_vms: list):
        if settings.deploy_mode == "dry_run":
            return

        logger.warning("Rollback del slice %s", request.slice_id)

        for vm in reversed(created_vms):
            try:
                with self._worker_client(vm.server) as client:
                    VMManager(client).delete_vm(vm.name, OVS_BRIDGE)
            except Exception as exc:
                logger.error("Rollback VM %s falló: %s", vm.name, exc)

        try:
            with self._headnode_client() as headnode:
                NetworkManager(headnode).delete_vlan_network(request.vlan_id, request.cidr)
        except Exception as exc:
            logger.error("Rollback red falló: %s", exc)

    # =========================
    # GRAPH MODE
    # =========================

    def _graph_tap_name(self, slice_id: str, node_name: str, iface_index: int) -> str:
    # Usamos 4 chars del hash del slice_id para hacerlos únicos entre slices.
        import hashlib
        slice_hash = hashlib.md5(slice_id.encode()).hexdigest()[:4]
        prefix = f"t{iface_index}{slice_hash}-"
        suffix_len = 15 - len(prefix)
        return prefix + node_name[-suffix_len:]

    def _graph_nat_tap_name(self, node_name: str) -> str:
        base = f"nt-{node_name}"
        return base[:15]


    def _graph_iface_ip(self, iface: dict) -> str | None:
        if iface.get("ip_cidr"):
            return str(ipaddress.ip_interface(iface["ip_cidr"]).ip)
        if iface.get("dhcp_reservation_ip"):
            return str(iface["dhcp_reservation_ip"])
        return None

    def _graph_shortest_path(self, adjacency: dict[str, set[str]], src: str, dst: str) -> list[str] | None:
        from collections import deque

        if src == dst:
            return [src]

        queue = deque([[src]])
        visited = {src}

        while queue:
            path = queue.popleft()
            current = path[-1]

            for neighbor in sorted(adjacency.get(current, set())):
                if neighbor in visited:
                    continue

                new_path = path + [neighbor]
                if neighbor == dst:
                    return new_path

                visited.add(neighbor)
                queue.append(new_path)

        return None

    def _add_unique_static_route(self, iface: dict, to: str, via: str):
        routes = iface.setdefault("static_routes", [])
        item = {"to": to, "via": via}
        if item not in routes:
            routes.append(item)

    def _apply_graph_routing(
        self,
        vlan_base: int,
        nodes: list[dict],
        links: list[dict],
        node_interfaces: dict[str, list[dict]],
    ):
        node_names = [node["name"] for node in nodes]
        node_by_name = {node["name"]: node for node in nodes}

        loopback_third_octet = vlan_base % 250
        if loopback_third_octet == 0:
            loopback_third_octet = 250

        for idx, node in enumerate(nodes, start=1):
            if idx > 254:
                raise ValueError("Demasiadas VMs para asignación loopback /32 simple")
            node["loopback_cidr"] = f"10.254.{loopback_third_octet}.{idx}/32"

        adjacency: dict[str, set[str]] = {name: set() for name in node_names}
        for link in links:
            a = link["from"]
            b = link["to"]
            adjacency.setdefault(a, set()).add(b)
            adjacency.setdefault(b, set()).add(a)

        def find_src_iface(src: str, neighbor: str) -> dict | None:
            return next((i for i in node_interfaces.get(src, []) if i.get("peer") == neighbor), None)

        def find_peer_ip(src: str, neighbor: str) -> str | None:
            peer_iface = next((i for i in node_interfaces.get(neighbor, []) if i.get("peer") == src), None)
            if not peer_iface:
                return None
            return self._graph_iface_ip(peer_iface)

        def target_ips_for_node(dst: str) -> list[str]:
            targets = [node_by_name[dst]["loopback_cidr"]]

            for iface in node_interfaces.get(dst, []):
                ip_value = self._graph_iface_ip(iface)
                if ip_value:
                    cidr = f"{ip_value}/32"
                    if cidr not in targets:
                        targets.append(cidr)

            return targets

        for src in node_names:
            for dst in node_names:
                if src == dst:
                    continue

                path = self._graph_shortest_path(adjacency, src, dst)
                if not path or len(path) < 2:
                    logger.warning("No hay camino entre %s y %s; no se agregan rutas", src, dst)
                    continue

                next_hop = path[1]
                src_iface = find_src_iface(src, next_hop)
                via_ip = find_peer_ip(src, next_hop)

                if not src_iface or not via_ip:
                    logger.warning("No se pudo calcular next-hop desde %s hacia %s", src, dst)
                    continue

                for target in target_ips_for_node(dst):
                    if target == f"{via_ip}/32":
                        continue
                    self._add_unique_static_route(src_iface, target, via_ip)



    def _graph_vm_number(self, name: str, fallback: int) -> int:
        digits = "".join(ch for ch in str(name) if ch.isdigit())
        if digits:
            value = int(digits)
            if 1 <= value <= 499:
                return value
        return fallback

    def _graph_ssh_user_for_node(self, node: dict) -> str:
        image = str(node.get("resolved_image_name") or node.get("image_name") or "").lower()
        if "cirros" in image:
            return "cirros"
        return "ubuntu"

    def _graph_ssh_credential_hint_for_node(self, node: dict) -> str | None:
        image = str(node.get("resolved_image_name") or node.get("image_name") or "").lower()
        if "cirros" in image:
            return "cirros / gocubsgo"
        if "ubuntu" in image:
            return "ubuntu / ubuntu"
        return None

    def _graph_nat_ip_for_node(self, node_name: str, node_interfaces: dict[str, list[dict]]) -> str | None:
        for iface in node_interfaces.get(node_name, []):
            if iface.get("link_id") != "nat":
                continue

            if iface.get("ip_cidr"):
                return str(ipaddress.ip_interface(iface["ip_cidr"]).ip)

            if iface.get("dhcp_reservation_ip"):
                return str(iface["dhcp_reservation_ip"])

        return None

    def _graph_worker_public_port(self, worker_name: str) -> int:
        import json
        import os
        import re

        raw = getattr(settings, "workers_json", "") or os.environ.get("WORKERS_JSON", "")

        try:
            workers = json.loads(raw or "[]")
            for item in workers:
                if str(item.get("name")) == str(worker_name):
                    return int(item.get("port") or item.get("ssh_port"))
        except Exception:
            pass

        m = re.search(r"(\d+)$", str(worker_name))
        if m:
            return 5810 + int(m.group(1))

        return int(getattr(settings, "headnode_ssh_port", 5814) or 5814)

    def _ensure_graph_ssh_forwards(
        self,
        headnode: SSHClient,
        nat_meta: dict | None,
        nodes: list[dict],
        node_interfaces: dict[str, list[dict]],
    ) -> list[dict]:
        """
        Publica SSH externo sin alojar VMs en el headnode.

        Flujo:
          cliente -> headnode:puerto_publico -> worker:puerto_worker -> VM:22
        """
        if not nat_meta or not nat_meta.get("enabled"):
            return []

        forwards: list[dict] = []
        used_ports: set[int] = set()

        headnode_ip = settings.headnode_ssh_host
        public_host = settings.linux_ssh_public_host or headnode_ip
        base_port = int(getattr(settings, "linux_ssh_start_port", 2200) or 2200)

        for idx, node in enumerate(nodes, start=1):
            if not node.get("internet"):
                continue

            vm_name = node["name"]
            worker_name = node["server"]
            vm_ip = self._graph_nat_ip_for_node(vm_name, node_interfaces)

            if not vm_ip:
                logger.warning("VM %s tiene internet=True pero no tiene IP NAT; no se publica SSH", vm_name)
                continue

            vm_num = self._graph_vm_number(vm_name, idx)
            ssh_port = base_port + vm_num

            while ssh_port in used_ports:
                ssh_port += 100

            used_ports.add(ssh_port)

            ssh_user = self._graph_ssh_user_for_node(node)
            credential_hint = self._graph_ssh_credential_hint_for_node(node)
            jump_user = getattr(settings, "headnode_ssh_user", "ubuntu") or "ubuntu"
            jump_host = public_host
            jump_port = self._graph_worker_public_port(worker_name)
            proxyjump_command = f"ssh -J {jump_user}@{jump_host}:{jump_port} {ssh_user}@{vm_ip}"
            proxyjump_endpoint = f"{jump_host}:{jump_port} -> {vm_ip}:22"

            with self._worker_client(worker_name) as worker_client:
                worker_ip_cmd = "ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i==\"src\") {print $(i+1); exit}}'"
                out, _ = worker_client.execute(worker_ip_cmd, raise_on_error=False)
                worker_ip = (out or "").strip().split()[0] if (out or "").strip() else ""

                try:
                    ipaddress.ip_address(worker_ip)
                except Exception as exc:
                    raise RuntimeError(f"No se pudo resolver IP de gestión del worker {worker_name}: {worker_ip!r}") from exc

                worker_cmds = [
                    f"iptables -t nat -D PREROUTING -p tcp --dport {ssh_port} -j DNAT --to-destination {vm_ip}:22 2>/dev/null || true",
                    f"iptables -t nat -D OUTPUT -p tcp -d {worker_ip} --dport {ssh_port} -j DNAT --to-destination {vm_ip}:22 2>/dev/null || true",
                    f"iptables -t nat -D POSTROUTING -p tcp -d {vm_ip} --dport 22 -j MASQUERADE 2>/dev/null || true",
                    f"iptables -D FORWARD -p tcp -d {vm_ip} --dport 22 -j ACCEPT 2>/dev/null || true",
                    f"iptables -D FORWARD -p tcp -s {vm_ip} --sport 22 -j ACCEPT 2>/dev/null || true",
                    "sysctl -w net.ipv4.ip_forward=1 >/dev/null",
                    f"iptables -t nat -A PREROUTING -p tcp --dport {ssh_port} -j DNAT --to-destination {vm_ip}:22",
                    f"iptables -t nat -A OUTPUT -p tcp -d {worker_ip} --dport {ssh_port} -j DNAT --to-destination {vm_ip}:22",
                    f"iptables -t nat -A POSTROUTING -p tcp -d {vm_ip} --dport 22 -j MASQUERADE",
                    f"iptables -I FORWARD -p tcp -d {vm_ip} --dport 22 -j ACCEPT",
                    f"iptables -I FORWARD -p tcp -s {vm_ip} --sport 22 -j ACCEPT",
                ]

                worker_client.sudo("bash -lc " + repr(" ; ".join(worker_cmds)))

            headnode_cmds = [
                f"iptables -t nat -D PREROUTING -p tcp --dport {ssh_port} -j DNAT --to-destination {vm_ip}:22 2>/dev/null || true",
                f"iptables -t nat -D OUTPUT -p tcp -d {headnode_ip} --dport {ssh_port} -j DNAT --to-destination {vm_ip}:22 2>/dev/null || true",
                f"iptables -t nat -D POSTROUTING -p tcp -d {vm_ip} --dport 22 -j MASQUERADE 2>/dev/null || true",
                f"iptables -D FORWARD -p tcp -d {vm_ip} --dport 22 -j ACCEPT 2>/dev/null || true",
                f"iptables -D FORWARD -p tcp -s {vm_ip} --sport 22 -j ACCEPT 2>/dev/null || true",
                f"iptables -t nat -D PREROUTING -p tcp --dport {ssh_port} -j DNAT --to-destination {worker_ip}:{ssh_port} 2>/dev/null || true",
                f"iptables -t nat -D OUTPUT -p tcp -d {headnode_ip} --dport {ssh_port} -j DNAT --to-destination {worker_ip}:{ssh_port} 2>/dev/null || true",
                f"iptables -t nat -D POSTROUTING -p tcp -d {worker_ip} --dport {ssh_port} -j MASQUERADE 2>/dev/null || true",
                f"iptables -D FORWARD -p tcp -d {worker_ip} --dport {ssh_port} -j ACCEPT 2>/dev/null || true",
                f"iptables -D FORWARD -p tcp -s {worker_ip} --sport {ssh_port} -j ACCEPT 2>/dev/null || true",
                "sysctl -w net.ipv4.ip_forward=1 >/dev/null",
                f"iptables -t nat -A PREROUTING -p tcp --dport {ssh_port} -j DNAT --to-destination {worker_ip}:{ssh_port}",
                f"iptables -t nat -A OUTPUT -p tcp -d {headnode_ip} --dport {ssh_port} -j DNAT --to-destination {worker_ip}:{ssh_port}",
                f"iptables -t nat -A POSTROUTING -p tcp -d {worker_ip} --dport {ssh_port} -j MASQUERADE",
                f"iptables -I FORWARD -p tcp -d {worker_ip} --dport {ssh_port} -j ACCEPT",
                f"iptables -I FORWARD -p tcp -s {worker_ip} --sport {ssh_port} -j ACCEPT",
            ]

            headnode.sudo("bash -lc " + repr(" ; ".join(headnode_cmds)))

            forwards.append(
                {
                    "vm": vm_name,
                    "target_ip": vm_ip,
                    "target_port": 22,
                    "headnode_ip": headnode_ip,
                    "public_host": public_host,
                    "ssh_port": ssh_port,
                    "ssh_user": ssh_user,
                    "ssh_command": proxyjump_command,
                    "external_endpoint": proxyjump_endpoint,
                    "credential_hint": credential_hint,
                    "access_mode": "proxyjump",
                    "jump_user": jump_user,
                    "jump_host": jump_host,
                    "jump_port": jump_port,
                    "worker": worker_name,
                    "worker_ip": worker_ip,
                    "worker_forward_port": ssh_port,
                    "published_direct_port": ssh_port,
                    "forward_mode": "headnode_to_worker_nat",
                }
            )

            logger.info(
                "SSH forward distribuido para %s: %s:%s -> %s:%s -> %s:22",
                vm_name,
                public_host,
                ssh_port,
                worker_name,
                ssh_port,
                vm_ip,
            )

        nat_meta["ssh_forwards"] = forwards
        return forwards

    def _remove_graph_ssh_forwards(self, headnode: SSHClient, forwards: list[dict] | None):
        for item in forwards or []:
            vm_ip = item.get("target_ip")
            ssh_port = item.get("ssh_port")
            headnode_ip = item.get("headnode_ip", settings.headnode_ssh_host)
            worker_name = item.get("worker")
            worker_ip = item.get("worker_ip")

            if not vm_ip or not ssh_port:
                continue

            if worker_name:
                try:
                    with self._worker_client(worker_name) as worker_client:
                        worker_cmds = [
                            f"iptables -t nat -D PREROUTING -p tcp --dport {ssh_port} -j DNAT --to-destination {vm_ip}:22 2>/dev/null || true",
                            f"iptables -t nat -D OUTPUT -p tcp -d {worker_ip} --dport {ssh_port} -j DNAT --to-destination {vm_ip}:22 2>/dev/null || true",
                            f"iptables -t nat -D POSTROUTING -p tcp -d {vm_ip} --dport 22 -j MASQUERADE 2>/dev/null || true",
                            f"iptables -D FORWARD -p tcp -d {vm_ip} --dport 22 -j ACCEPT 2>/dev/null || true",
                            f"iptables -D FORWARD -p tcp -s {vm_ip} --sport 22 -j ACCEPT 2>/dev/null || true",
                        ]
                        worker_client.sudo("bash -lc " + repr(" ; ".join(worker_cmds)), raise_on_error=False)
                except Exception as exc:
                    logger.warning("No se pudo remover SSH forward en worker %s: %s", worker_name, exc)

            headnode_cmds = [
                f"iptables -t nat -D PREROUTING -p tcp --dport {ssh_port} -j DNAT --to-destination {vm_ip}:22 2>/dev/null || true",
                f"iptables -t nat -D OUTPUT -p tcp -d {headnode_ip} --dport {ssh_port} -j DNAT --to-destination {vm_ip}:22 2>/dev/null || true",
                f"iptables -t nat -D POSTROUTING -p tcp -d {vm_ip} --dport 22 -j MASQUERADE 2>/dev/null || true",
                f"iptables -D FORWARD -p tcp -d {vm_ip} --dport 22 -j ACCEPT 2>/dev/null || true",
                f"iptables -D FORWARD -p tcp -s {vm_ip} --sport 22 -j ACCEPT 2>/dev/null || true",
            ]

            if worker_ip:
                headnode_cmds.extend(
                    [
                        f"iptables -t nat -D PREROUTING -p tcp --dport {ssh_port} -j DNAT --to-destination {worker_ip}:{ssh_port} 2>/dev/null || true",
                        f"iptables -t nat -D OUTPUT -p tcp -d {headnode_ip} --dport {ssh_port} -j DNAT --to-destination {worker_ip}:{ssh_port} 2>/dev/null || true",
                        f"iptables -t nat -D POSTROUTING -p tcp -d {worker_ip} --dport {ssh_port} -j MASQUERADE 2>/dev/null || true",
                        f"iptables -D FORWARD -p tcp -d {worker_ip} --dport {ssh_port} -j ACCEPT 2>/dev/null || true",
                        f"iptables -D FORWARD -p tcp -s {worker_ip} --sport {ssh_port} -j ACCEPT 2>/dev/null || true",
                    ]
                )

            headnode.sudo("bash -lc " + repr(" ; ".join(headnode_cmds)), raise_on_error=False)
            logger.info("SSH forward removido: puerto %s hacia %s", ssh_port, vm_ip)

    def _remove_graph_worker_gateways(self, nat: dict | None):
        """
        Cleanup robusto del NAT/DHCP creado en workers para Internet graph Linux.
        Limpia dnsmasq, iptables, puerto OVS e interfaz Linux del gateway NAT.
        """
        if not nat:
            return

        import shlex

        forwards = nat.get("ssh_forwards") or []
        gateways = list(nat.get("worker_gateways") or [])

        known_workers = {g.get("worker") for g in gateways if g.get("worker")}
        for fw in forwards:
            worker = fw.get("worker")
            if worker and worker not in known_workers:
                gateways.append(
                    {
                        "worker": worker,
                        "ifname": nat.get("ifname"),
                        "subnet_cidr": nat.get("subnet_cidr"),
                        "reservations": nat.get("reservations") or [],
                    }
                )
                known_workers.add(worker)

        if not gateways:
            logger.info("No hay worker_gateways que limpiar para NAT graph")
            return

        forwards_by_worker: dict[str, list[dict]] = {}
        for fw in forwards:
            worker = fw.get("worker")
            if worker:
                forwards_by_worker.setdefault(worker, []).append(fw)

        for gateway in gateways:
            worker_name = gateway.get("worker")
            if not worker_name:
                continue

            ifname = gateway.get("ifname") or nat.get("ifname") or ""
            subnet_cidr = gateway.get("subnet_cidr") or nat.get("subnet_cidr") or ""
            reservations = gateway.get("reservations") or nat.get("reservations") or []

            ips = [str(item.get("ip")) for item in reservations if item.get("ip")]
            ports: list[int] = []
            worker_ip = ""

            for fw in forwards_by_worker.get(worker_name, []):
                if fw.get("worker_ip"):
                    worker_ip = str(fw.get("worker_ip"))

                if fw.get("worker_forward_port"):
                    ports.append(int(fw.get("worker_forward_port")))
                elif fw.get("ssh_port"):
                    ports.append(int(fw.get("ssh_port")))

                if fw.get("target_ip") and str(fw.get("target_ip")) not in ips:
                    ips.append(str(fw.get("target_ip")))

            ifname_q = shlex.quote(ifname)
            subnet_q = shlex.quote(subnet_cidr)
            worker_ip_q = shlex.quote(worker_ip)
            ips_q = shlex.quote(" ".join(sorted(set(ips))))
            ports_q = shlex.quote(" ".join(str(p) for p in sorted(set(ports))))

            shell = f"""
set +e

OUT_IF="$(ip route get 1.1.1.1 2>/dev/null | awk '{{for(i=1;i<=NF;i++) if ($i=="dev") {{print $(i+1); exit}}}}')"
[ -n "$OUT_IF" ] || OUT_IF=ens3

IFNAME={ifname_q}
SUBNET={subnet_q}
WORKER_IP={worker_ip_q}
IPS={ips_q}
PORTS={ports_q}

if [ -n "$IFNAME" ]; then
  PIDFILE="/var/run/dnsmasq-${{IFNAME}}.pid"
  if [ -f "$PIDFILE" ]; then
    PID="$(cat "$PIDFILE" 2>/dev/null || true)"
    case "$PID" in
      ''|*[!0-9]*) ;;
      *)
        if ps -p "$PID" -o args= 2>/dev/null | grep -q 'dnsmasq'; then
          sudo kill "$PID" 2>/dev/null || true
        fi
        ;;
    esac
  fi

  sudo rm -f "/var/run/dnsmasq-${{IFNAME}}.conf"              "/var/run/dnsmasq-${{IFNAME}}.pid"              "/var/run/dnsmasq-${{IFNAME}}.leases"              "/var/log/dnsmasq-${{IFNAME}}.log" || true
fi

for PORT in $PORTS; do
  for IP in $IPS; do
    while sudo iptables -t nat -D PREROUTING -p tcp --dport "$PORT" -j DNAT --to-destination "$IP":22 2>/dev/null; do :; done
    while sudo iptables -t nat -D POSTROUTING -d "$IP"/32 -p tcp --dport 22 -j MASQUERADE 2>/dev/null; do :; done
    while sudo iptables -D FORWARD -d "$IP"/32 -p tcp --dport 22 -j ACCEPT 2>/dev/null; do :; done
    while sudo iptables -D FORWARD -s "$IP"/32 -p tcp --sport 22 -j ACCEPT 2>/dev/null; do :; done

    if [ -n "$WORKER_IP" ]; then
      while sudo iptables -t nat -D OUTPUT -d "$WORKER_IP"/32 -p tcp --dport "$PORT" -j DNAT --to-destination "$IP":22 2>/dev/null; do :; done
    fi
  done
done

for IP in $IPS; do
  while sudo iptables -t nat -D POSTROUTING -s "$IP"/32 -o "$OUT_IF" -j MASQUERADE 2>/dev/null; do :; done
  while sudo iptables -D FORWARD -d "$IP"/32 -j ACCEPT 2>/dev/null; do :; done
  while sudo iptables -D FORWARD -s "$IP"/32 -j ACCEPT 2>/dev/null; do :; done
done

if [ -n "$SUBNET" ]; then
  while sudo iptables -t nat -D POSTROUTING -s "$SUBNET" -o "$OUT_IF" -j MASQUERADE 2>/dev/null; do :; done
fi

if [ -n "$IFNAME" ]; then
  while sudo iptables -D FORWARD -i "$IFNAME" -o "$OUT_IF" -j ACCEPT 2>/dev/null; do :; done
  while sudo iptables -D FORWARD -i "$OUT_IF" -o "$IFNAME" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null; do :; done

  sudo ovs-vsctl --if-exists del-port br-int "$IFNAME" || true
  sudo ip link del "$IFNAME" 2>/dev/null || true
fi

exit 0
"""

            try:
                with self._worker_client(worker_name) as worker_client:
                    worker_client.sudo("bash -lc " + repr(shell), raise_on_error=False)
                    logger.info("Worker gateway NAT removido en %s: %s", worker_name, ifname)
            except Exception as exc:
                logger.warning("No se pudo remover worker gateway NAT en %s: %s", worker_name, exc)


    def _build_graph_interfaces(self, slice_id: str, nodes: list[dict], links: list[dict], vlan_base: int, internet_mode: str):
        # Usar vlan_base como índice de partida en el pool de subnets,
        # garantizando que cada slice empiece en una posición distinta
        # y nunca choque con otros slices activos
        _static_all = ipaddress.ip_network("10.200.0.0/16").subnets(new_prefix=30)
        static_pool = (s for i, s in enumerate(_static_all) if i >= vlan_base)
        _dhcp_all = ipaddress.ip_network("10.201.0.0/16").subnets(new_prefix=29)
        dhcp_pool = (s for i, s in enumerate(_dhcp_all) if i >= vlan_base)

        node_by_name = {node["name"]: node for node in nodes}
        node_interfaces: dict[str, list[dict]] = {node["name"]: [] for node in nodes}
        iface_counter: dict[str, int] = {node["name"]: 0 for node in nodes}

        links_out = []
        dhcp_networks = []

        for offset, link in enumerate(links):
            vlan_id = vlan_base + offset
            a = link["from"]
            b = link["to"]
            a_node = node_by_name[a]
            b_node = node_by_name[b]

            link_has_cirros = self._node_is_cirros(a_node) or self._node_is_cirros(b_node)

            if link_has_cirros:
                subnet = next(dhcp_pool)
                hosts = list(subnet.hosts())
                gw_ip = str(hosts[0])
                a_ip = str(hosts[1])
                b_ip = str(hosts[2])
                dhcp_ifname = f"dhcp{vlan_id}"[:15]
            else:
                subnet = next(static_pool)
                hosts = list(subnet.hosts())
                gw_ip = None
                a_ip = str(hosts[0])
                b_ip = str(hosts[1])
                dhcp_ifname = None

            a_idx = iface_counter[a]
            a_tap = self._graph_tap_name(slice_id, a, a_idx)
            a_mac = self._stable_mac(f"{slice_id}:{a}:{link['id']}:{a_tap}")
            a_payload = {
                "link_id": link["id"],
                "peer": b,
                "vlan_id": vlan_id,
                "tap_name": a_tap,
                "mac_address": a_mac,
            }
            if self._node_is_cirros(a_node):
                a_payload["dhcp_reservation_ip"] = a_ip
            else:
                a_payload["ip_cidr"] = f"{a_ip}/{subnet.prefixlen}"
            node_interfaces[a].append(a_payload)
            iface_counter[a] += 1

            b_idx = iface_counter[b]
            b_tap = self._graph_tap_name(slice_id, b, b_idx)
            b_mac = self._stable_mac(f"{slice_id}:{b}:{link['id']}:{b_tap}")
            b_payload = {
                "link_id": link["id"],
                "peer": a,
                "vlan_id": vlan_id,
                "tap_name": b_tap,
                "mac_address": b_mac,
            }
            if self._node_is_cirros(b_node):
                b_payload["dhcp_reservation_ip"] = b_ip
            else:
                b_payload["ip_cidr"] = f"{b_ip}/{subnet.prefixlen}"
            node_interfaces[b].append(b_payload)
            iface_counter[b] += 1

            links_out.append({"id": link["id"], "from": a, "to": b, "vlan_id": vlan_id})

            if link_has_cirros:
                dhcp_networks.append(
                    {
                        "kind": "link",
                        "ifname": dhcp_ifname,
                        "vlan_id": vlan_id,
                        "subnet_cidr": str(subnet),
                        "gateway_ip_cidr": f"{gw_ip}/{subnet.prefixlen}",
                        "provide_router": False,
                        "provide_dns": False,
                        "enable_nat": False,
                        "reservations": [
                            {"mac": a_mac, "ip": a_ip, "name": a},
                            {"mac": b_mac, "ip": b_ip, "name": b},
                        ],
                    }
                )

        nat_meta = None
        internet_nodes = [n for n in nodes if n.get("internet")]

        if internet_mode == "headnode_nat" and internet_nodes:
            nat_vlan_id = vlan_base + len(links) + 1
            octet = nat_vlan_id % 250
            if octet == 0:
                octet = 250

            nat_subnet = f"172.31.{octet}.0/24"
            nat_gateway = f"172.31.{octet}.1"
            nat_gateway_cidr = f"{nat_gateway}/24"
            nat_ifname = f"nat{nat_vlan_id}"[:15]

            ip_host = 10
            nat_reservations = []
            use_dhcp = False

            for node in nodes:
                if not node.get("internet"):
                    continue

                tap_name = self._graph_nat_tap_name(node["name"])
                mac = self._stable_mac(f"{slice_id}:{node['name']}:nat:{tap_name}")
                assigned_ip = f"172.31.{octet}.{ip_host}"

                nat_payload = {
                    "link_id": "nat",
                    "peer": "headnode_nat",
                    "vlan_id": nat_vlan_id,
                    "tap_name": tap_name,
                    "mac_address": mac,
                }

                if self._node_is_cirros(node):
                    use_dhcp = True
                    nat_payload["dhcp_reservation_ip"] = assigned_ip
                    nat_reservations.append({"mac": mac, "ip": assigned_ip, "name": node["name"]})
                else:
                    nat_payload["ip_cidr"] = f"{assigned_ip}/24"
                    nat_payload["gateway4"] = nat_gateway
                    nat_payload["nameservers"] = ["8.8.8.8", "1.1.1.1"]

                node_interfaces[node["name"]].append(nat_payload)
                ip_host += 1

            nat_meta = {
                "enabled": True,
                "ifname": nat_ifname,
                "vlan_id": nat_vlan_id,
                "subnet_cidr": nat_subnet,
                "gateway_ip_cidr": nat_gateway_cidr,
                "use_dhcp": use_dhcp,
                "reservations": nat_reservations,
            }

        elif internet_mode == "provider_network" and internet_nodes:
            PROVIDER_SUBNET = "10.60.11.0/24"
            PROVIDER_GATEWAY = "10.60.11.1"
            PROVIDER_VLAN_ID = 0  # sin tag — cambiar cuando Branko confirme el VLAN ID
            ip_host = 10
            for node in nodes:
                if not node.get("internet"):
                    continue
                tap_name = self._graph_nat_tap_name(node["name"])
                mac = self._stable_mac(f"{slice_id}:{node['name']}:provider:{tap_name}")
                assigned_ip = f"10.60.11.{ip_host}"
                nat_payload = {
                    "link_id": "provider",
                    "peer": "gateway",
                    "vlan_id": PROVIDER_VLAN_ID,
                    "tap_name": tap_name,
                    "mac_address": mac,
                    "ip_cidr": f"{assigned_ip}/24",
                    "gateway4": PROVIDER_GATEWAY,
                    "nameservers": ["8.8.8.8", "1.1.1.1"],
                }
                node_interfaces[node["name"]].append(nat_payload)
                ip_host += 1
            nat_meta = {
                "enabled": True,
                "mode": "provider_network",
                "subnet_cidr": PROVIDER_SUBNET,
                "gateway_ip_cidr": f"{PROVIDER_GATEWAY}/24",
                "use_dhcp": False,
                "reservations": [],
            }

        self._apply_graph_routing(vlan_base, nodes, links, node_interfaces)
        return links_out, node_interfaces, nat_meta, dhcp_networks

    def create_graph_slice(self, request: dict) -> dict:
        slice_id = request["slice_id"]
        nodes = request["nodes"]
        links = request["links"]
        vlan_base = request["vlan_base"]
        network_backend = request.get("network_backend", "vlan")
        internet_mode = request.get("internet_mode", "none")

        if network_backend != "vlan":
            return {
                "slice_id": slice_id,
                "success": False,
                "vms": [],
                "links": [],
                "nat": None,
                "dhcp": [],
                "nodes": nodes,
                "error": "Por ahora graph mode solo soporta network_backend=vlan",
            }

        if settings.deploy_mode != "dry_run":
            nodes = self._sync_graph_images(nodes)

        links_out, node_interfaces, nat_meta, dhcp_networks = self._build_graph_interfaces(
            slice_id=slice_id,
            nodes=nodes,
            links=links,
            vlan_base=vlan_base,
            internet_mode=internet_mode,
        )

        # Mapa nodo→worker (todos los nodos, no solo los ya creados). Se usa para
        # ubicar el dnsmasq y para que el rollback limpie el DHCP en el worker
        # correcto aunque falle antes de crear todas las VMs.
        graph_node_servers = {node["name"]: node["server"] for node in nodes}

        if settings.deploy_mode == "dry_run":
            vms = []
            for node in nodes:
                vms.append(
                    self._graph_vm_payload(
                        node=node,
                        status="planned",
                        error=None,
                        interfaces=node_interfaces[node["name"]],
                    )
                )
            return {
                "slice_id": slice_id,
                "success": True,
                "vms": vms,
                "links": links_out,
                "nat": nat_meta,
                "dhcp": dhcp_networks,
                "nodes": nodes,
                "error": None,
            }

        created_nodes = []
        vm_results = []
        ssh_forwards_by_vm: dict[str, dict] = {}
        nat_ready = False
        dhcp_ready: list[dict] = []

        try:
            # Internet headnode_nat se prepara en el worker donde vive la VM.
            # El headnode no aloja slices; solo publica el puerto externo.
            if nat_meta and nat_meta.get("enabled"):
                if nat_meta.get("mode") == "provider_network":
                    nat_ready = True
                else:
                    reservations_by_name = {
                        item.get("name"): item
                        for item in nat_meta.get("reservations", [])
                        if item.get("name")
                    }

                    internet_workers: dict[str, list[dict]] = {}
                    for node in nodes:
                        if node.get("internet"):
                            internet_workers.setdefault(node["server"], []).append(node)

                    worker_gateways = []

                    for worker_name, worker_nodes in internet_workers.items():
                        worker_reservations = [
                            reservations_by_name[node["name"]]
                            for node in worker_nodes
                            if node["name"] in reservations_by_name
                        ]

                        with self._worker_client(worker_name) as worker_client:
                            worker_net_mgr = NetworkManager(worker_client)

                            if nat_meta.get("use_dhcp"):
                                worker_net_mgr.ensure_vlan_dhcp(
                                    ifname=nat_meta["ifname"],
                                    vlan_id=nat_meta["vlan_id"],
                                    subnet_cidr=nat_meta["subnet_cidr"],
                                    gateway_ip_cidr=nat_meta["gateway_ip_cidr"],
                                    reservations=worker_reservations,
                                    provide_router=True,
                                    provide_dns=True,
                                    enable_nat=True,
                                )
                            else:
                                worker_net_mgr.ensure_headnode_nat(
                                    ifname=nat_meta["ifname"],
                                    vlan_id=nat_meta["vlan_id"],
                                    headnode_ip_cidr=nat_meta["gateway_ip_cidr"],
                                    subnet_cidr=nat_meta["subnet_cidr"],
                                )

                        worker_gateways.append(
                            {
                                "worker": worker_name,
                                "ifname": nat_meta["ifname"],
                                "vlan_id": nat_meta["vlan_id"],
                                "subnet_cidr": nat_meta["subnet_cidr"],
                                "gateway_ip_cidr": nat_meta["gateway_ip_cidr"],
                                "use_dhcp": nat_meta.get("use_dhcp", False),
                                "reservations": worker_reservations,
                            }
                        )

                    nat_meta["worker_gateways"] = worker_gateways
                    nat_ready = True

            # DHCP por worker: el dnsmasq se instala en el mismo worker donde
            # vive cada VM. El headnode NO tiene trunk hacia los workers —
            # el broadcast DHCP no cruza entre OVS bridges aislados, así que
            # el dnsmasq debe correr local en el host que tiene la VM.
            #
            # Lógica: por cada dhcp_network, identificamos qué workers tienen
            # VMs participantes en ese link (via reservations[].name → node →
            # server), e instalamos el dnsmasq en cada uno de esos workers.
            # Resultado: cada worker tiene su propio dnsmasq para las VLANs
            # que le corresponden, y el broadcast nunca necesita cruzar.
            #
            # IMPORTANTE (fix): un enlace cuyos dos extremos caen en workers
            # distintos comparte una única VLAN que, gracias al trunk ens4, es
            # un solo dominio de broadcast entre workers (es el mismo motivo por
            # el que el ARP/ping cruza). Por eso instalamos el dnsmasq en UN SOLO
            # worker "dueño" por red DHCP: el DISCOVER del extremo remoto llega
            # por el trunk. Instalarlo en cada worker duplicaba el servidor DHCP
            # y la IP .1 del gateway en el mismo segmento L2 (conflicto de ARP).
            # El dueño se elige de forma determinística (worker de menor nombre)
            # para que el borrado/rollback lo puedan recalcular.
            _dhcp_workers: dict[str, list[dict]] = {}  # worker_name → [dhcp, ...]
            _node_to_server = graph_node_servers
            for _dhcp in dhcp_networks:
                _candidate_workers = sorted({
                    _node_to_server.get(_res.get("name", ""))
                    for _res in _dhcp.get("reservations", [])
                    if _node_to_server.get(_res.get("name", ""))
                })
                if not _candidate_workers:
                    continue
                _owner = _candidate_workers[0]
                _dhcp_workers.setdefault(_owner, []).append(_dhcp)

            for _worker_name, _worker_dhcps in _dhcp_workers.items():
                try:
                    with self._worker_client(_worker_name) as _wclient:
                        _wnet = NetworkManager(_wclient)
                        for _dhcp in _worker_dhcps:
                            _wnet.ensure_vlan_dhcp(
                                ifname=_dhcp["ifname"],
                                vlan_id=_dhcp["vlan_id"],
                                subnet_cidr=_dhcp["subnet_cidr"],
                                gateway_ip_cidr=_dhcp["gateway_ip_cidr"],
                                reservations=_dhcp["reservations"],
                                provide_router=_dhcp.get("provide_router", False),
                                provide_dns=_dhcp.get("provide_dns", False),
                                enable_nat=_dhcp.get("enable_nat", False),
                            )
                            if _dhcp not in dhcp_ready:
                                dhcp_ready.append(_dhcp)
                    logger.info("DHCP instalado en worker %s para VLANs %s",
                                _worker_name,
                                [d["vlan_id"] for d in _worker_dhcps])
                except Exception as _exc:
                    logger.warning("No se pudo instalar DHCP en worker %s: %s",
                                   _worker_name, _exc)

            with self._headnode_client() as headnode:
                net_mgr = NetworkManager(headnode)

                if nat_meta and nat_meta.get("enabled") and nat_meta.get("mode") != "provider_network":
                    forwards = self._ensure_graph_ssh_forwards(
                        headnode=headnode,
                        nat_meta=nat_meta,
                        nodes=nodes,
                        node_interfaces=node_interfaces,
                    )
                    ssh_forwards_by_vm = {item["vm"]: item for item in forwards}

            for node in nodes:
                name = node["name"]
                server = node["server"]
                vnc_port = node["vnc_port"]

                interfaces = []
                is_cirros = self._node_is_cirros(node)
                for idx, iface in enumerate(node_interfaces[name]):
                    guest_name = f"eth{idx}" if is_cirros else f"ens{idx + 3}"
                    interfaces.append(
                        VMInterface(
                            tap_name=iface["tap_name"],
                            vlan_id=iface["vlan_id"],
                            ip_cidr=iface.get("ip_cidr"),
                            mac_address=iface.get("mac_address"),
                            gateway4=iface.get("gateway4"),
                            nameservers=iface.get("nameservers", []),
                            set_name=guest_name,
                            static_routes=iface.get("static_routes", []),
                        )
                    )

                try:
                    with self._worker_client(server) as client:
                        mgr = VMManager(client)
                        cfg = VMConfig(
                            name=name,
                            ovs_bridge=OVS_BRIDGE,
                            vnc_port=vnc_port,
                            base_image=node.get("resolved_image_name", node.get("image_name", "cirros-base.img")),
                            ram_mb=node.get("ram_mb", 256),
                            vcpus=node.get("vcpus", 1),
                            disk_gb=node.get("disk_gb", 10),
                            interfaces=interfaces,
                            loopback_cidr=node.get("loopback_cidr"),
                            enable_ip_forward=True,
                            slice_id=slice_id, 
                        )
                        mgr.create_vm(cfg)
                        # Retry hasta 15s — QEMU daemonize puede cambiar el PID
                        status = "stopped"
                        for _ in range(5):
                            time.sleep(3)
                            status = mgr.get_vm_status(name, slice_id)
                            if status == "running":
                                break
                        if status != "running":
                            raise RuntimeError(f"La VM {name} no quedó running; estado={status}")

                    vm_results.append(
                        self._graph_vm_payload(
                            node=node,
                            status=status,
                            error=None,
                            interfaces=node_interfaces[name],
                            ssh_forward=ssh_forwards_by_vm.get(name),
                        )
                    )
                    created_nodes.append({**node, "interfaces": node_interfaces[name]})

                except Exception as exc:
                    logger.exception("Error creando nodo %s", name)
                    vm_results.append(
                        self._graph_vm_payload(
                            node=node,
                            status="error",
                            error=str(exc),
                            interfaces=node_interfaces[name],
                            ssh_forward=ssh_forwards_by_vm.get(name),
                        )
                    )
                    raise

            # R6 — switch dinámico: limitar broadcast por enlace virtual
            # Cada VLAN se instala solo en los workers que realmente tienen VMs conectadas a ese enlace.
            try:
                node_by_name = {node['name']: node for node in nodes}
                vlan_workers: dict[int, set[str]] = {}

                # Para cada enlace virtual, calcular exactamente qué workers participan.
                for link in links_out:
                    vlan_id = int(link['vlan_id'])
                    workers = vlan_workers.setdefault(vlan_id, set())

                    for endpoint in (link.get('from'), link.get('to')):
                        node = node_by_name.get(endpoint)
                        if node and node.get('server'):
                            workers.add(node['server'])

                # Si hay NAT/Internet, la VLAN de NAT solo se instala en workers con VMs que requieren internet.
                if nat_meta and nat_meta.get('enabled') and nat_meta.get('vlan_id'):
                    nat_vlan = int(nat_meta['vlan_id'])
                    nat_workers = {
                        gateway.get('worker')
                        for gateway in nat_meta.get('worker_gateways', [])
                        if gateway.get('worker')
                    }

                    if not nat_workers:
                        nat_workers = {
                            node.get('server')
                            for node in nodes
                            if node.get('internet') and node.get('server')
                        }

                    if nat_workers:
                        vlan_workers.setdefault(nat_vlan, set()).update(nat_workers)

                # Aplicar reglas OVS por VLAN únicamente en los workers necesarios.
                for vlan_id, workers in sorted(vlan_workers.items()):
                    worker_list = sorted(workers)
                    for worker_name in worker_list:
                        with self._worker_client(worker_name) as worker_client:
                            NetworkManager(worker_client).apply_slice_flows(slice_id, [vlan_id])

                    print(
                        f"[LINUX_SWITCH] VLAN {vlan_id} limitada a workers={worker_list}",
                        flush=True,
                    )

                # El headnode solo recibe las VLANs donde participa por DHCP/NAT.
                headnode_vlans = {int(item['vlan_id']) for item in dhcp_networks if item.get('vlan_id') is not None}
                if nat_meta and nat_meta.get('enabled') and nat_meta.get('mode') != 'provider_network' and nat_meta.get('vlan_id'):
                    headnode_vlans.add(int(nat_meta['vlan_id']))

                if headnode_vlans:
                    with self._headnode_client() as headnode:
                        NetworkManager(headnode).apply_slice_flows(slice_id, sorted(headnode_vlans))
                    print(
                        f"[LINUX_SWITCH] VLANs del headnode para DHCP/NAT slice={slice_id}, vlans={sorted(headnode_vlans)}",
                        flush=True,
                    )

                print(
                    f"[LINUX_SECURITY] Seguridad/broadcast aplicado para slice={slice_id}, "
                    f"vlan_workers={{v: sorted(w) for v, w in vlan_workers.items()}}",
                    flush=True,
                )

            except Exception as exc:
                logger.warning("No se pudieron aplicar reglas dinámicas de broadcast para %s: %s", slice_id, exc)

            return {
                "slice_id": slice_id,
                "success": True,
                "vms": vm_results,
                "links": links_out,
                "nat": nat_meta,
                "dhcp": dhcp_networks,
                "nodes": nodes,
                "error": None,
            }

        except Exception as exc:
            logger.exception("Fallo creando graph slice %s", slice_id)
            self._rollback_graph(created_nodes, nat_meta if nat_ready else None, dhcp_ready, slice_id=slice_id, node_servers=graph_node_servers)
            return {
                "slice_id": slice_id,
                "success": False,
                "vms": vm_results,
                "links": links_out,
                "nat": nat_meta,
                "dhcp": dhcp_networks,
                "nodes": nodes,
                "error": str(exc),
            }

    def delete_graph_slice(self, slice_id: str, vms: list[dict], nat: dict | None = None, dhcp: list[dict] | None = None) -> dict:
        # block2.17 early worker gateway cleanup
        self._remove_graph_worker_gateways(nat)

        if settings.deploy_mode == "dry_run":
            return {"slice_id": slice_id, "success": True, "mode": "dry_run"}

        success = True

        for vm in vms:
            try:
                with self._worker_client(vm["server"]) as client:
                    taps = [iface["tap_name"] for iface in vm.get("interfaces", [])]
                    VMManager(client).delete_vm(vm["name"], OVS_BRIDGE, taps, slice_id=slice_id)
            except Exception as exc:
                logger.error("Error borrando graph VM %s: %s", vm["name"], exc)
                success = False

        try:
            with self._headnode_client() as headnode:
                net_mgr = NetworkManager(headnode)

                if nat and nat.get("enabled"):
                    if nat.get("mode") == "provider_network":
                        pass  # nada que limpiar en el headnode
                    else:
                        self._remove_graph_ssh_forwards(headnode, nat.get("ssh_forwards", []))
                        self._remove_graph_worker_gateways(nat)
                        if nat.get("use_dhcp"):
                            net_mgr.remove_vlan_dhcp(
                                ifname=nat["ifname"],
                                subnet_cidr=nat["subnet_cidr"],
                                vlan_id=nat["vlan_id"],
                            )
                        else:
                            net_mgr.remove_headnode_nat(
                                ifname=nat["ifname"],
                                subnet_cidr=nat["subnet_cidr"],
                            )

                # DHCP se instaló en workers (no headnode) — limpiar en cada worker.
                _del_node_to_server = {vm.get("name", ""): vm.get("server", "") for vm in vms}
                _del_dhcp_workers: dict[str, list[dict]] = {}
                for _item in dhcp or []:
                    _seen_w: set[str] = set()
                    for _res in _item.get("reservations", []):
                        _w = _del_node_to_server.get(_res.get("name", ""), "")
                        if _w and _w not in _seen_w:
                            _del_dhcp_workers.setdefault(_w, []).append(_item)
                            _seen_w.add(_w)
                for _w_name, _w_items in _del_dhcp_workers.items():
                    try:
                        with self._worker_client(_w_name) as _wc:
                            _wnet = NetworkManager(_wc)
                            for _item in _w_items:
                                _wnet.remove_vlan_dhcp(
                                    ifname=_item["ifname"],
                                    subnet_cidr=_item["subnet_cidr"],
                                    vlan_id=_item["vlan_id"],
                                )
                    except Exception as _exc:
                        logger.warning("No se pudo limpiar DHCP en worker %s: %s", _w_name, _exc)

                # R6 — eliminar reglas dinámicas de broadcast del slice
                try:
                    vlan_workers: dict[int, set[str]] = {}

                    for vm in vms:
                        worker_name = vm.get('server')
                        if not worker_name:
                            continue

                        for iface in vm.get('interfaces', []):
                            if iface.get('vlan_id') is not None:
                                vlan_workers.setdefault(int(iface['vlan_id']), set()).add(worker_name)

                    if nat and nat.get('enabled') and nat.get('vlan_id'):
                        nat_vlan = int(nat['vlan_id'])
                        nat_workers = {
                            gateway.get('worker')
                            for gateway in nat.get('worker_gateways', [])
                            if gateway.get('worker')
                        }
                        if nat_workers:
                            vlan_workers.setdefault(nat_vlan, set()).update(nat_workers)

                    for vlan_id, workers in sorted(vlan_workers.items()):
                        worker_list = sorted(workers)
                        for worker_name in worker_list:
                            with self._worker_client(worker_name) as worker_client:
                                NetworkManager(worker_client).remove_slice_flows(slice_id, [vlan_id])

                        print(
                            f"[LINUX_SWITCH] VLAN {vlan_id} eliminada de workers={worker_list}",
                            flush=True,
                        )

                    headnode_vlans = {int(item['vlan_id']) for item in dhcp or [] if item.get('vlan_id') is not None}
                    if nat and nat.get('enabled') and nat.get('mode') != 'provider_network' and nat.get('vlan_id'):
                        headnode_vlans.add(int(nat['vlan_id']))

                    if headnode_vlans:
                        net_mgr.remove_slice_flows(slice_id, sorted(headnode_vlans))
                        print(
                            f"[LINUX_SWITCH] VLANs del headnode eliminadas para slice={slice_id}, vlans={sorted(headnode_vlans)}",
                            flush=True,
                        )

                    print(
                        f"[LINUX_SECURITY] Seguridad/broadcast eliminada para slice={slice_id}, "
                        f"vlan_workers={{v: sorted(w) for v, w in vlan_workers.items()}}",
                        flush=True,
                    )

                except Exception as exc:
                    logger.warning("No se pudieron eliminar reglas dinámicas de broadcast para %s: %s", slice_id, exc)
        except Exception as exc:
            logger.error("Error borrando DHCP/NAT graph: %s", exc)
            success = False

        return {"slice_id": slice_id, "success": success}

    def _rollback_graph(self, created_nodes: list[dict], nat: dict | None = None, dhcp: list[dict] | None = None, slice_id: str = "", node_servers: dict | None = None):
        for node in reversed(created_nodes):
            try:
                with self._worker_client(node["server"]) as client:
                    taps = [iface["tap_name"] for iface in node.get("interfaces", [])]
                    VMManager(client).delete_vm(node["name"], OVS_BRIDGE, taps, slice_id=slice_id)
            except Exception as exc:
                logger.error("Rollback graph VM %s falló: %s", node["name"], exc)

        # NAT/SSH forwards viven en el headnode/workers-gateway: se limpian aparte.
        try:
            with self._headnode_client() as headnode:
                net_mgr = NetworkManager(headnode)
                if nat and nat.get("enabled") and nat.get("mode") != "provider_network":
                    self._remove_graph_ssh_forwards(headnode, nat.get("ssh_forwards", []))
                    self._remove_graph_worker_gateways(nat)
                    if nat.get("use_dhcp"):
                        net_mgr.remove_vlan_dhcp(
                            ifname=nat["ifname"],
                            subnet_cidr=nat["subnet_cidr"],
                            vlan_id=nat["vlan_id"],
                        )
                    else:
                        net_mgr.remove_headnode_nat(
                            ifname=nat["ifname"],
                            subnet_cidr=nat["subnet_cidr"],
                        )
        except Exception as exc:
            logger.error("Rollback NAT falló: %s", exc)

        # FIX: el DHCP se instala en los WORKERS (no en el headnode), así que el
        # rollback también debe limpiarlo ahí. Se limpia en todos los workers que
        # alojen un extremo de cada red DHCP (superconjunto del worker "dueño"):
        # remove_vlan_dhcp es idempotente, en un worker sin dnsmasq es un no-op.
        _rb_node_to_server = dict(node_servers or {})
        if not _rb_node_to_server:
            _rb_node_to_server = {n.get("name", ""): n.get("server", "") for n in created_nodes}

        _rb_dhcp_workers: dict[str, list[dict]] = {}
        for _item in dhcp or []:
            for _res in _item.get("reservations", []):
                _w = _rb_node_to_server.get(_res.get("name", ""), "")
                if _w and _item not in _rb_dhcp_workers.setdefault(_w, []):
                    _rb_dhcp_workers[_w].append(_item)

        for _w_name, _w_items in _rb_dhcp_workers.items():
            try:
                with self._worker_client(_w_name) as _wc:
                    _wnet = NetworkManager(_wc)
                    for _item in _w_items:
                        _wnet.remove_vlan_dhcp(
                            ifname=_item["ifname"],
                            subnet_cidr=_item["subnet_cidr"],
                            vlan_id=_item["vlan_id"],
                        )
            except Exception as _exc:
                logger.warning("Rollback: no se pudo limpiar DHCP en worker %s: %s", _w_name, _exc)

    def action_graph_vm(self, vm: dict, action: str) -> dict:
        action = (action or "").strip().lower()
        if action not in {"start", "stop", "reboot", "pause", "resume"}:
            raise ValueError(f"Acción no soportada: {action}")

        # slice_id es necesario para que vm_manager encuentre los paths
        # correctos (launcher, pid, monitor) que tienen el formato
        # "{slice_id}--{vm_name}.sh". Sin él busca "vm1.sh" en vez de
        # "test-linux-01--vm1.sh" y lanza FileNotFoundError.
        slice_id = vm.get("slice_id", "")

        with self._worker_client(vm["server"]) as client:
            mgr = VMManager(client)

            if action == "start":
                mgr.start_vm(vm["name"], slice_id)
            elif action == "stop":
                mgr.stop_vm(vm["name"], slice_id=slice_id)
            elif action == "reboot":
                mgr.reboot_vm(vm["name"], slice_id)
            elif action == "pause":
                mgr.pause_vm(vm["name"], slice_id=slice_id)
            elif action == "resume":
                mgr.resume_vm(vm["name"], slice_id=slice_id)

            time.sleep(1)
            status = mgr.get_vm_status(vm["name"], slice_id)

        return {
            "vm_name": vm["name"],
            "server": vm["server"],
            "action": action,
            "status": status,
        }