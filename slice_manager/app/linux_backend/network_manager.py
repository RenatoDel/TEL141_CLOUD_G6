from __future__ import annotations

import base64
import ipaddress

OVS_BRIDGE = "br-int"
UPLINK_IF = "ens3"


class NetworkManager:
    def __init__(self, ssh):
        self.ssh = ssh

    def _sudo_ok(self, cmd: str):
        self.ssh.sudo(cmd)

    def _sudo_try(self, cmd: str):
        self.ssh.sudo(cmd, raise_on_error=False)

    def _sudo_bash(self, script: str):
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        self.ssh.sudo(f"bash -lc \"$(printf %s '{encoded}' | base64 -d)\"")

    def _write_remote_text(self, path: str, content: str):
        from pathlib import Path

        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        parent = str(Path(path).parent)

        cmd = (
            f"mkdir -p {parent} && "
            f"echo {b64} | base64 -d > {path}"
        )
        self._sudo_bash(cmd)

    def _iptables_apply(self, rules: list[str]):
        script = "set -e\n"
        script += 'IPT="$(command -v iptables || command -v iptables-legacy || command -v iptables-nft)"\n'
        script += '[ -n "$IPT" ] || { echo "iptables no encontrado"; exit 1; }\n'
        for rule in rules:
            script += f'{rule}\n'
        self._sudo_bash(script)

    def _iptables_try(self, rules: list[str]):
        script = 'IPT="$(command -v iptables || command -v iptables-legacy || command -v iptables-nft)"\n'
        script += '[ -n "$IPT" ] || exit 0\n'
        for rule in rules:
            script += f'{rule} || true\n'
        self._sudo_bash(script)

    def _dnsmasq_paths(self, ifname: str):
        conf = f"/var/run/dnsmasq-{ifname}.conf"
        pid = f"/var/run/dnsmasq-{ifname}.pid"
        leases = f"/var/run/dnsmasq-{ifname}.leases"
        log = f"/var/log/dnsmasq-{ifname}.log"
        return conf, pid, leases, log

    def create_vlan_network(
        self,
        vlan_id: int,
        cidr: str,
        has_dhcp: bool = False,
        dhcp_start: str | None = None,
        dhcp_end: str | None = None,
    ):
        net = ipaddress.ip_network(cidr, strict=False)
        gw = f"{net.network_address + 1}/{net.prefixlen}"
        ifname = f"vlan{vlan_id}"

        self._sudo_ok(
            f"ovs-vsctl --may-exist add-port {OVS_BRIDGE} {ifname} tag={vlan_id} -- "
            f"set interface {ifname} type=internal"
        )
        self._sudo_try(f"ip addr flush dev {ifname}")
        self._sudo_ok(f"ip link set {ifname} up")
        self._sudo_ok(f"ip addr add {gw} dev {ifname}")

    def ensure_vlan_dhcp(
        self,
        ifname: str,
        vlan_id: int,
        subnet_cidr: str,
        gateway_ip_cidr: str,
        reservations: list[dict] | None = None,
        provide_router: bool = False,
        provide_dns: bool = False,
        enable_nat: bool = False,
    ):
        reservations = reservations or []

        net = ipaddress.ip_network(subnet_cidr, strict=False)
        gw_ip = str(ipaddress.ip_interface(gateway_ip_cidr).ip)
        hosts = list(net.hosts())
        if len(hosts) < 3:
            raise RuntimeError(f"La red {subnet_cidr} no tiene espacio suficiente para DHCP")

        range_start = str(hosts[1])
        range_end = str(hosts[-1])

        self._sudo_ok("mkdir -p /var/run /var/log /tmp /var/tmp")
        self._sudo_try("chmod 1777 /tmp /var/tmp")
        self._sudo_bash(
            "command -v dnsmasq >/dev/null 2>&1 || "
            "(apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y dnsmasq-base)"
        )

        self._sudo_ok(
            f"ovs-vsctl --may-exist add-port {OVS_BRIDGE} {ifname} tag={vlan_id} -- "
            f"set interface {ifname} type=internal"
        )
        self._sudo_try(f"ip addr flush dev {ifname}")
        self._sudo_ok(f"ip link set {ifname} up")
        self._sudo_ok(f"ip addr add {gateway_ip_cidr} dev {ifname}")

        conf_path, pid_path, lease_path, log_path = self._dnsmasq_paths(ifname)

        lines = [
            "bind-interfaces",
            f"interface={ifname}",
            "except-interface=lo",
            "dhcp-authoritative",
            f"dhcp-range={range_start},{range_end},{net.netmask},1h",
        ]

        if provide_router or enable_nat:
            lines.append(f"dhcp-option=3,{gw_ip}")
        else:
            lines.append("dhcp-option=3")

        if provide_dns or enable_nat:
            lines.append("dhcp-option=6,8.8.8.8,1.1.1.1")

        for item in reservations:
            mac = item.get("mac")
            ip = item.get("ip")
            name = item.get("name", ip or "host")
            if mac and ip:
                lines.append(f"dhcp-host={mac},{ip},{name},1h")

        conf_text = "\n".join(lines) + "\n"
        self._write_remote_text(conf_path, conf_text)

        self._sudo_try(f"pkill -F {pid_path}")
        self._sudo_try(f"pkill -f 'dnsmasq --conf-file={conf_path}'")
        self._sudo_try(f"rm -f {lease_path}")

        self._sudo_ok(
            f"dnsmasq --conf-file={conf_path} "
            f"--pid-file={pid_path} "
            f"--dhcp-leasefile={lease_path} "
            f"--log-facility={log_path}"
        )

        if enable_nat:
            self._sudo_ok("sysctl -w net.ipv4.ip_forward=1")
            self._iptables_apply([
                f'$IPT -t nat -C POSTROUTING -s {subnet_cidr} -o {UPLINK_IF} -j MASQUERADE || $IPT -t nat -A POSTROUTING -s {subnet_cidr} -o {UPLINK_IF} -j MASQUERADE',
                f'$IPT -C FORWARD -i {ifname} -o {UPLINK_IF} -j ACCEPT || $IPT -A FORWARD -i {ifname} -o {UPLINK_IF} -j ACCEPT',
                f'$IPT -C FORWARD -i {UPLINK_IF} -o {ifname} -m state --state RELATED,ESTABLISHED -j ACCEPT || $IPT -A FORWARD -i {UPLINK_IF} -o {ifname} -m state --state RELATED,ESTABLISHED -j ACCEPT',
            ])

    def remove_vlan_dhcp(self, ifname: str, vlan_id: int, subnet_cidr: str):
        conf_path, pid_path, lease_path, _ = self._dnsmasq_paths(ifname)

        self._sudo_try(f"pkill -F {pid_path}")
        self._sudo_try(f"pkill -f 'dnsmasq --conf-file={conf_path}'")
        self._sudo_try(f"rm -f {pid_path} {lease_path} {conf_path}")

        self._iptables_try([
            f'$IPT -t nat -D POSTROUTING -s {subnet_cidr} -o {UPLINK_IF} -j MASQUERADE',
            f'$IPT -D FORWARD -i {ifname} -o {UPLINK_IF} -j ACCEPT',
            f'$IPT -D FORWARD -i {UPLINK_IF} -o {ifname} -m state --state RELATED,ESTABLISHED -j ACCEPT',
        ])

        self._sudo_try(f"ip addr flush dev {ifname}")
        self._sudo_try(f"ovs-vsctl --if-exists del-port {OVS_BRIDGE} {ifname}")
        self._sudo_try(f"ip link delete {ifname}")

    def enable_internet(self, vlan_id: int, cidr: str):
        ifname = f"vlan{vlan_id}"
        self._sudo_ok("sysctl -w net.ipv4.ip_forward=1")
        self._iptables_apply([
            f'$IPT -t nat -C POSTROUTING -s {cidr} -o {UPLINK_IF} -j MASQUERADE || $IPT -t nat -A POSTROUTING -s {cidr} -o {UPLINK_IF} -j MASQUERADE',
            f'$IPT -C FORWARD -i {ifname} -o {UPLINK_IF} -j ACCEPT || $IPT -A FORWARD -i {ifname} -o {UPLINK_IF} -j ACCEPT',
            f'$IPT -C FORWARD -i {UPLINK_IF} -o {ifname} -m state --state RELATED,ESTABLISHED -j ACCEPT || $IPT -A FORWARD -i {UPLINK_IF} -o {ifname} -m state --state RELATED,ESTABLISHED -j ACCEPT',
        ])

    def delete_vlan_network(self, vlan_id: int, cidr: str):
        self.remove_vlan_dhcp(f"vlan{vlan_id}", vlan_id, cidr)

    def ensure_headnode_nat(
        self,
        ifname: str,
        vlan_id: int,
        headnode_ip_cidr: str,
        subnet_cidr: str,
    ):
        self._sudo_ok(
            f"ovs-vsctl --may-exist add-port {OVS_BRIDGE} {ifname} tag={vlan_id} -- "
            f"set interface {ifname} type=internal"
        )
        self._sudo_try(f"ip addr flush dev {ifname}")
        self._sudo_ok(f"ip link set {ifname} up")
        self._sudo_ok(f"ip addr add {headnode_ip_cidr} dev {ifname}")

        self._sudo_ok("sysctl -w net.ipv4.ip_forward=1")
        self._iptables_apply([
            f'$IPT -t nat -C POSTROUTING -s {subnet_cidr} -o {UPLINK_IF} -j MASQUERADE || $IPT -t nat -A POSTROUTING -s {subnet_cidr} -o {UPLINK_IF} -j MASQUERADE',
            f'$IPT -C FORWARD -i {ifname} -o {UPLINK_IF} -j ACCEPT || $IPT -A FORWARD -i {ifname} -o {UPLINK_IF} -j ACCEPT',
            f'$IPT -C FORWARD -i {UPLINK_IF} -o {ifname} -m state --state RELATED,ESTABLISHED -j ACCEPT || $IPT -A FORWARD -i {UPLINK_IF} -o {ifname} -m state --state RELATED,ESTABLISHED -j ACCEPT',
        ])

    def remove_headnode_nat(self, ifname: str, subnet_cidr: str):
        self._iptables_try([
            f'$IPT -t nat -D POSTROUTING -s {subnet_cidr} -o {UPLINK_IF} -j MASQUERADE',
            f'$IPT -D FORWARD -i {ifname} -o {UPLINK_IF} -j ACCEPT',
            f'$IPT -D FORWARD -i {UPLINK_IF} -o {ifname} -m state --state RELATED,ESTABLISHED -j ACCEPT',
        ])

        self._sudo_try(f"ip addr flush dev {ifname}")
        self._sudo_try(f"ovs-vsctl --if-exists del-port {OVS_BRIDGE} {ifname}")
        self._sudo_try(f"ip link delete {ifname}")

    # -------------------------------------------------------------------------
    # R5 — OVS flow isolation
    # -------------------------------------------------------------------------

    def _ovs_cookie(self, slice_id: str, vlan_id: int) -> str:
        import hashlib
        h = int(hashlib.md5(f"{slice_id}:{vlan_id}".encode()).hexdigest()[:8], 16)
        return f"0x{h:08x}"

    def apply_slice_flows(self, slice_id: str, vlan_ids: list[int]):
        """
        Aplica reglas de seguridad L2 para un slice Linux.
        
        Reglas:
        - Permite tráfico únicamente en las VLANs asignadas al slice.
        - Cada regla queda marcada con un cookie derivado del slice_id.
        - Bloquea tráfico sin VLAN para evitar fugas entre slices.
        """
        if not vlan_ids:
            return

        vlan_ids = sorted(set(int(v) for v in vlan_ids))
        cmds = []

        for vlan_id in vlan_ids:
            cookie = self._ovs_cookie(slice_id, vlan_id)

            cmds.append(
                f"ovs-ofctl add-flow {OVS_BRIDGE} "
                f"\"cookie={cookie},priority=200,dl_vlan={vlan_id},arp,actions=normal\""
            )

            cmds.append(
                f"ovs-ofctl add-flow {OVS_BRIDGE} "
                f"\"cookie={cookie},priority=200,dl_vlan={vlan_id},ip,actions=normal\""
            )

            cmds.append(
                f"ovs-ofctl add-flow {OVS_BRIDGE} "
                f"\"cookie={cookie},priority=100,dl_vlan={vlan_id},actions=normal\""
            )

        # Drop defensivo para tráfico sin etiqueta VLAN.
        drop_cookie = "0x00000001"
        cmds.append(
            f"ovs-ofctl add-flow {OVS_BRIDGE} "
            f"\"cookie={drop_cookie},priority=10,dl_vlan=0xffff,actions=drop\" 2>/dev/null || true"
        )

        script = "set -e\n" + "\n".join(cmds)
        self._sudo_bash(script)

        print(
            f"[LINUX_SECURITY] Reglas OVS aplicadas para slice={slice_id}, "
            f"vlans={vlan_ids}",
            flush=True,
        )

    def remove_slice_flows(self, slice_id: str, vlan_ids: list[int]):
        """
        Elimina las reglas OVS asociadas al slice usando sus cookies.
        """
        if not vlan_ids:
            return

        vlan_ids = sorted(set(int(v) for v in vlan_ids))
        cmds = []

        for vlan_id in vlan_ids:
            cookie = self._ovs_cookie(slice_id, vlan_id)
            cmds.append(
                f"ovs-ofctl del-flows {OVS_BRIDGE} \"cookie={cookie}/-1\" 2>/dev/null || true"
            )

        script = "\n".join(cmds)
        self._sudo_bash(script)

        print(
            f"[LINUX_SECURITY] Reglas OVS eliminadas para slice={slice_id}, "
            f"vlans={vlan_ids}",
            flush=True,
        )
