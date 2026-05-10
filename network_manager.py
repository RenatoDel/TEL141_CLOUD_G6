from __future__ import annotations
from __future__ import annotations
"""
network_manager.py
------------------
Gestión de red para slices en la topología VNRT.
Basado en create_network_vlan.sh, internet_to_network.sh,
routing_networks.sh y sus variantes de disable del laboratorio 4.

Arquitectura de red real:
    - server1, server2: cómputo — tienen br-int con ens4 conectado
    - server3         : headnode — gestiona VLANs, DHCP, iptables, internet
    - OFS             : switch OpenFlow que conecta todos los servidores

Cada slice tiene una VLAN ID única que aísla su tráfico.
El headnode (server3) crea puertos internos OVS por VLAN
y actúa como gateway para las VMs.
"""

import logging
from typing import Optional

from ssh_client import SSHClient

logger = logging.getLogger(__name__)

OVS_BRIDGE    = "br-int"    # bridge OVS en todos los servidores
INTERNET_IFACE = "ens3"     # interfaz de salida a internet (red de acceso)
                             # NO agregar al OVS — penalización en el curso


class NetworkManager:
    """
    Gestiona la red de un slice en el headnode (server3).
    El headnode es quien crea los gateways VLAN y las reglas iptables.

    Uso:
        with SSHClient(...) as headnode_client:
            mgr = NetworkManager(headnode_client)
            mgr.create_vlan_network(vlan_id=100, cidr="192.168.100.0/24")
            mgr.enable_internet(vlan_id=100, cidr="192.168.100.0/24")
            mgr.delete_vlan_network(vlan_id=100, cidr="192.168.100.0/24")
    """

    def __init__(self, ssh: SSHClient):
        self.ssh = ssh

    # ------------------------------------------------------------------
    # Crear red VLAN — replica create_network_vlan.sh
    # ------------------------------------------------------------------

    def create_vlan_network(
        self,
        vlan_id:    int,
        cidr:       str,
        dhcp:       bool = False,
        dhcp_start: Optional[str] = None,
        dhcp_end:   Optional[str] = None,
    ):
        """
        Crea una red VLAN en el headnode.
        Replica create_network_vlan.sh exactamente.

        Parámetros:
            vlan_id    : VLAN ID del slice (ej: 100, 200)
            cidr       : red en formato CIDR (ej: 192.168.100.0/24)
            dhcp       : si True crea namespace DHCP con dnsmasq
            dhcp_start : primera IP del rango DHCP
            dhcp_end   : última IP del rango DHCP

        Ejemplo sin DHCP (IPs manuales en las VMs):
            mgr.create_vlan_network(100, "192.168.100.0/24")

        Ejemplo con DHCP:
            mgr.create_vlan_network(
                200, "192.168.200.0/24",
                dhcp=True,
                dhcp_start="192.168.200.10",
                dhcp_end="192.168.200.100"
            )
        """
        logger.info(
            f"[NetworkManager] Creando red VLAN {vlan_id} "
            f"CIDR={cidr} dhcp={dhcp}"
        )

        # Calcular IPs desde CIDR (igual que en el script bash)
        network, prefix = cidr.split("/")
        octets = network.split(".")
        base   = int(octets[3])
        gw_ip  = f"{octets[0]}.{octets[1]}.{octets[2]}.{base + 1}"
        dhcp_ip= f"{octets[0]}.{octets[1]}.{octets[2]}.{base + 2}"

        iface_name = f"vlan{vlan_id}"

        logger.debug(f"[NetworkManager] Gateway: {gw_ip}/{prefix}")

        # 1. Crear puerto interno OVS para la VLAN
        ports_out, _ = self.ssh.sudo(
            f"ovs-vsctl list-ports {OVS_BRIDGE}",
            raise_on_error=False,
        )
        if iface_name not in ports_out.split():
            self.ssh.sudo(
                f"ovs-vsctl add-port {OVS_BRIDGE} {iface_name} "
                f"tag={vlan_id} -- set Interface {iface_name} type=internal"
            )
            logger.debug(f"[NetworkManager] Puerto interno {iface_name} creado")
        else:
            logger.debug(f"[NetworkManager] Puerto {iface_name} ya existe")

        # Activar interfaz
        self.ssh.sudo(f"ip link set {iface_name} up")

        # Asignar IP gateway
        ip_check, _ = self.ssh.execute(
            f"ip addr show {iface_name} | grep '{gw_ip}' || echo missing",
            raise_on_error=False,
        )
        if "missing" in ip_check:
            self.ssh.sudo(f"ip addr add {gw_ip}/{prefix} dev {iface_name}")
            logger.debug(f"[NetworkManager] IP {gw_ip}/{prefix} asignada a {iface_name}")

        # 2. Configurar DHCP si se pide
        if dhcp:
            if not dhcp_start or not dhcp_end:
                raise ValueError(
                    "Para DHCP debes indicar dhcp_start y dhcp_end"
                )
            self._setup_dhcp(vlan_id, cidr, gw_ip, dhcp_ip, prefix, dhcp_start, dhcp_end)

        logger.info(f"[NetworkManager] Red VLAN {vlan_id} lista. Gateway: {gw_ip}")

    def _setup_dhcp(
        self,
        vlan_id:    int,
        cidr:       str,
        gw_ip:      str,
        dhcp_ip:    str,
        prefix:     str,
        dhcp_start: str,
        dhcp_end:   str,
    ):
        """
        Configura DHCP en un namespace Linux con dnsmasq.
        Replica la parte DHCP de create_network_vlan.sh.
        """
        ns_name   = f"dhcp-ns-{vlan_id}"
        veth_host = f"veth-h-{vlan_id}"
        veth_ns   = f"veth-ns-{vlan_id}"

        logger.debug(f"[NetworkManager] Configurando DHCP en namespace {ns_name}")

        # Crear namespace
        ns_list, _ = self.ssh.sudo("ip netns list", raise_on_error=False)
        if ns_name not in ns_list:
            self.ssh.sudo(f"ip netns add {ns_name}")

        # Crear par veth
        out, _ = self.ssh.execute(
            f"ip link show {veth_host} 2>/dev/null && echo exists || echo missing",
            raise_on_error=False,
        )
        if "missing" in out:
            self.ssh.sudo(
                f"ip link add {veth_host} type veth peer name {veth_ns}"
            )

        # Mover extremo NS al namespace
        ns_check, _ = self.ssh.execute(
            f"sudo ip netns exec {ns_name} ip link show {veth_ns} "
            f"2>/dev/null && echo exists || echo missing",
            raise_on_error=False,
        )
        if "missing" in ns_check:
            self.ssh.sudo(f"ip link set {veth_ns} netns {ns_name}")

        # Conectar extremo host al OVS con tag VLAN
        ports_out, _ = self.ssh.sudo(
            f"ovs-vsctl list-ports {OVS_BRIDGE}", raise_on_error=False
        )
        if veth_host not in ports_out.split():
            self.ssh.sudo(
                f"ovs-vsctl add-port {OVS_BRIDGE} {veth_host} tag={vlan_id}"
            )
        self.ssh.sudo(f"ip link set {veth_host} up")

        # Configurar IP en el extremo NS
        self.ssh.sudo(f"ip netns exec {ns_name} ip link set {veth_ns} up")
        self.ssh.sudo(f"ip netns exec {ns_name} ip link set lo up")
        ip_check, _ = self.ssh.execute(
            f"sudo ip netns exec {ns_name} ip addr show {veth_ns} "
            f"| grep '{dhcp_ip}' || echo missing",
            raise_on_error=False,
        )
        if "missing" in ip_check:
            self.ssh.sudo(
                f"ip netns exec {ns_name} ip addr add {dhcp_ip}/{prefix} dev {veth_ns}"
            )

        # Ruta default dentro del namespace
        self.ssh.sudo(
            f"ip netns exec {ns_name} ip route replace default via {gw_ip}",
            raise_on_error=False,
        )

        # Lanzar dnsmasq
        pid_file = f"/var/run/dnsmasq-{ns_name}.pid"
        if self.ssh.file_exists(pid_file):
            self.ssh.execute(
                f"sudo kill $(cat {pid_file}) 2>/dev/null || true",
                raise_on_error=False,
            )
            self.ssh.sudo(f"rm -f {pid_file}", raise_on_error=False)

        self.ssh.sudo(
            f"ip netns exec {ns_name} dnsmasq "
            f"--interface={veth_ns} "
            f"--bind-interfaces "
            f"--dhcp-range={dhcp_start},{dhcp_end},255.255.255.0,12h "
            f"--dhcp-option=3,{gw_ip} "
            f"--dhcp-option=6,8.8.8.8 "
            f"--no-resolv "
            f"--pid-file={pid_file} "
            f"--log-facility=/var/log/dnsmasq-vlan{vlan_id}.log"
        )
        logger.info(f"[NetworkManager] DHCP activo en namespace {ns_name}")

    # ------------------------------------------------------------------
    # Internet — replica internet_to_network.sh
    # ------------------------------------------------------------------

    def enable_internet(self, vlan_id: int, cidr: str):
        """
        Habilita salida a internet para una VLAN via NAT iptables.
        Replica internet_to_network.sh.
        """
        logger.info(f"[NetworkManager] Habilitando internet para VLAN {vlan_id}")
        vlan_iface = f"vlan{vlan_id}"

        # Activar forwarding
        self.ssh.sudo("sysctl -w net.ipv4.ip_forward=1", raise_on_error=False)

        # MASQUERADE
        self._iptables_add_if_missing(
            f"-t nat -A POSTROUTING -s {cidr} -o {INTERNET_IFACE} -j MASQUERADE",
            f"-t nat -C POSTROUTING -s {cidr} -o {INTERNET_IFACE} -j MASQUERADE",
        )
        # FORWARD saliente
        self._iptables_add_if_missing(
            f"-A FORWARD -s {cidr} -o {INTERNET_IFACE} -j ACCEPT",
            f"-C FORWARD -s {cidr} -o {INTERNET_IFACE} -j ACCEPT",
        )
        # FORWARD retorno
        self._iptables_add_if_missing(
            f"-A FORWARD -i {INTERNET_IFACE} -d {cidr} -m state --state ESTABLISHED,RELATED -j ACCEPT",
            f"-C FORWARD -i {INTERNET_IFACE} -d {cidr} -m state --state ESTABLISHED,RELATED -j ACCEPT",
        )
        # Por iface VLAN
        self._iptables_add_if_missing(
            f"-A FORWARD -i {vlan_iface} -o {INTERNET_IFACE} -j ACCEPT",
            f"-C FORWARD -i {vlan_iface} -o {INTERNET_IFACE} -j ACCEPT",
        )
        self._iptables_add_if_missing(
            f"-A FORWARD -i {INTERNET_IFACE} -o {vlan_iface} -m state --state ESTABLISHED,RELATED -j ACCEPT",
            f"-C FORWARD -i {INTERNET_IFACE} -o {vlan_iface} -m state --state ESTABLISHED,RELATED -j ACCEPT",
        )
        logger.info(f"[NetworkManager] Internet habilitado para VLAN {vlan_id}")

    def disable_internet(self, vlan_id: int, cidr: str):
        """Deshabilita internet para una VLAN. Replica disable_internet_to_network.sh."""
        logger.info(f"[NetworkManager] Deshabilitando internet para VLAN {vlan_id}")
        vlan_iface = f"vlan{vlan_id}"

        self._iptables_del_if_exists(
            f"-t nat -D POSTROUTING -s {cidr} -o {INTERNET_IFACE} -j MASQUERADE",
            f"-t nat -C POSTROUTING -s {cidr} -o {INTERNET_IFACE} -j MASQUERADE",
        )
        self._iptables_del_if_exists(
            f"-D FORWARD -s {cidr} -o {INTERNET_IFACE} -j ACCEPT",
            f"-C FORWARD -s {cidr} -o {INTERNET_IFACE} -j ACCEPT",
        )
        self._iptables_del_if_exists(
            f"-D FORWARD -i {INTERNET_IFACE} -d {cidr} -m state --state ESTABLISHED,RELATED -j ACCEPT",
            f"-C FORWARD -i {INTERNET_IFACE} -d {cidr} -m state --state ESTABLISHED,RELATED -j ACCEPT",
        )
        self._iptables_del_if_exists(
            f"-D FORWARD -i {vlan_iface} -o {INTERNET_IFACE} -j ACCEPT",
            f"-C FORWARD -i {vlan_iface} -o {INTERNET_IFACE} -j ACCEPT",
        )
        self._iptables_del_if_exists(
            f"-D FORWARD -i {INTERNET_IFACE} -o {vlan_iface} -m state --state ESTABLISHED,RELATED -j ACCEPT",
            f"-C FORWARD -i {INTERNET_IFACE} -o {vlan_iface} -m state --state ESTABLISHED,RELATED -j ACCEPT",
        )

    # ------------------------------------------------------------------
    # Ruteo entre VLANs — replica routing_networks.sh
    # ------------------------------------------------------------------

    def enable_routing_between_vlans(self, vlan_id_1: int, vlan_id_2: int):
        """Permite ruteo entre dos VLANs. Replica routing_networks.sh."""
        logger.info(
            f"[NetworkManager] Habilitando ruteo VLAN {vlan_id_1} <-> VLAN {vlan_id_2}"
        )
        iface1 = f"vlan{vlan_id_1}"
        iface2 = f"vlan{vlan_id_2}"

        self._iptables_add_if_missing(
            f"-A FORWARD -i {iface1} -o {iface2} -j ACCEPT",
            f"-C FORWARD -i {iface1} -o {iface2} -j ACCEPT",
        )
        self._iptables_add_if_missing(
            f"-A FORWARD -i {iface2} -o {iface1} -j ACCEPT",
            f"-C FORWARD -i {iface2} -o {iface1} -j ACCEPT",
        )

    def disable_routing_between_vlans(self, vlan_id_1: int, vlan_id_2: int):
        """Elimina ruteo entre VLANs. Replica disable_routing_networks.sh."""
        iface1 = f"vlan{vlan_id_1}"
        iface2 = f"vlan{vlan_id_2}"

        self._iptables_del_if_exists(
            f"-D FORWARD -i {iface1} -o {iface2} -j ACCEPT",
            f"-C FORWARD -i {iface1} -o {iface2} -j ACCEPT",
        )
        self._iptables_del_if_exists(
            f"-D FORWARD -i {iface2} -o {iface1} -j ACCEPT",
            f"-C FORWARD -i {iface2} -o {iface1} -j ACCEPT",
        )

    # ------------------------------------------------------------------
    # Eliminar red VLAN completa
    # ------------------------------------------------------------------

    def delete_vlan_network(self, vlan_id: int, cidr: str):
        """
        Elimina completamente una red VLAN del headnode:
        - Deshabilita internet
        - Mata dnsmasq
        - Elimina namespace
        - Elimina interfaces veth
        - Elimina puerto interno OVS
        """
        logger.info(f"[NetworkManager] Eliminando red VLAN {vlan_id}")

        self.disable_internet(vlan_id, cidr)

        ns_name   = f"dhcp-ns-{vlan_id}"
        veth_host = f"veth-h-{vlan_id}"
        iface_name= f"vlan{vlan_id}"
        pid_file  = f"/var/run/dnsmasq-{ns_name}.pid"

        # Matar dnsmasq
        if self.ssh.file_exists(pid_file):
            self.ssh.execute(
                f"sudo kill $(cat {pid_file}) 2>/dev/null || true",
                raise_on_error=False,
            )
            self.ssh.sudo(f"rm -f {pid_file}", raise_on_error=False)

        # Eliminar namespace
        ns_list, _ = self.ssh.sudo("ip netns list", raise_on_error=False)
        if ns_name in ns_list:
            self.ssh.sudo(f"ip netns del {ns_name}", raise_on_error=False)

        # Eliminar veth del OVS
        ports_out, _ = self.ssh.sudo(
            f"ovs-vsctl list-ports {OVS_BRIDGE}", raise_on_error=False
        )
        if veth_host in ports_out.split():
            self.ssh.sudo(f"ovs-vsctl del-port {OVS_BRIDGE} {veth_host}", raise_on_error=False)

        # Eliminar interfaz interna OVS
        if iface_name in ports_out.split():
            self.ssh.sudo(f"ovs-vsctl del-port {OVS_BRIDGE} {iface_name}", raise_on_error=False)

        logger.info(f"[NetworkManager] Red VLAN {vlan_id} eliminada")

    # ------------------------------------------------------------------
    # Helpers iptables
    # ------------------------------------------------------------------

    def _iptables_add_if_missing(self, add_rule: str, check_rule: str):
        """Agrega una regla iptables solo si no existe."""
        check_out, _ = self.ssh.execute(
            f"sudo iptables {check_rule} 2>/dev/null && echo exists || echo missing",
            raise_on_error=False,
        )
        if "missing" in check_out:
            self.ssh.sudo(f"iptables {add_rule}")

    def _iptables_del_if_exists(self, del_rule: str, check_rule: str):
        """Elimina una regla iptables solo si existe."""
        check_out, _ = self.ssh.execute(
            f"sudo iptables {check_rule} 2>/dev/null && echo exists || echo missing",
            raise_on_error=False,
        )
        if "exists" in check_out:
            self.ssh.sudo(f"iptables {del_rule}", raise_on_error=False)
