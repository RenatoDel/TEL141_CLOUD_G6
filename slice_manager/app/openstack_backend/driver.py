"""
Driver de OpenStack con la misma interfaz que LinuxDriver.
Implementa: create_graph_slice, delete_graph_slice, action_graph_vm

Flujo interno (por slice):
  1. Keystone  → token admin
  2. Keystone  → crear proyecto para el slice
  3. Keystone  → asociar userG6 al proyecto con rol member
  4. Keystone  → token scoped al proyecto
  5. Neutron   → crear network + subnet por cada link
  6. Neutron   → crear puertos (uno por VM por link)
  7. Nova      → crear instancias con los puertos asignados
  8. Nova      → obtener URL de consola noVNC
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger("openstack_driver")

# ─────────────────────────────────────────────────────────────
# Configuración — se lee del .env via config.py del slice_manager
# ─────────────────────────────────────────────────────────────
import os

def OS_AUTH_URL(): return os.environ.get("OS_AUTH_URL", "http://controller:5000/v3")
def OS_USERNAME(): return os.environ.get("OS_USERNAME", "cloud_admin")
def OS_PASSWORD(): return os.environ.get("OS_PASSWORD", "66c5f106f03328bbb47bd5ec609c320e")
def OS_PROJECT_NAME(): return os.environ.get("OS_PROJECT_NAME", "cloud_admin")
def OS_USER_DOMAIN_NAME(): return os.environ.get("OS_USER_DOMAIN_NAME", "Cloud")
def OS_PROJECT_DOMAIN_NAME(): return os.environ.get("OS_PROJECT_DOMAIN_NAME", "Cloud")
def OS_DOMAIN_NAME(): return os.environ.get("OS_DOMAIN_NAME", "Cloud")
def OS_DOMAIN_ID(): return os.environ.get("OS_DOMAIN_ID", "ff80f00b054f4c4abd3a00d3de1bf48f")
def OS_SLICE_USER(): return os.environ.get("OS_SLICE_USER", "userG6")
def OS_SLICE_USER_PASSWORD(): return os.environ.get("OS_SLICE_USER_PASSWORD", "userG6")
def OS_FLAVOR_ID(): return os.environ.get("OS_FLAVOR_ID", "eb0bdaf9-4803-415c-8857-7956fefead50")
def OS_IMAGE_NAME(): return os.environ.get("OS_IMAGE_NAME", "cirros")

TIMEOUT = 60.0
VM_ACTIVE_TIMEOUT = 120  # segundos máximos esperando ACTIVE
VM_POLL_INTERVAL = 3


# ─────────────────────────────────────────────────────────────
# Cliente HTTP base
# ─────────────────────────────────────────────────────────────

class OpenStackClient:
    """
    Cliente HTTP liviano para las APIs de OpenStack.
    Maneja autenticación, tokens y reintentos básicos.
    """

    def __init__(self):
        self._admin_token: Optional[str] = None
        self._scoped_token: Optional[str] = None
        self._project_id: Optional[str] = None
        self._endpoints: dict[str, str] = {}

    # ── Keystone ──────────────────────────────────────────────

    def get_admin_token(self) -> str:
        """Obtiene token administrativo del dominio Cloud. Siempre fresco."""
        payload = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": OS_USERNAME(),
                            "password": OS_PASSWORD(),
                            "domain": {"name": OS_USER_DOMAIN_NAME()},
                        }
                    },
                },
                "scope": {
                    "project": {
                        "name": OS_PROJECT_NAME(),
                        "domain": {"name": OS_PROJECT_DOMAIN_NAME()},
                    }
                },
            }
        }

        r = httpx.post(
            f"{OS_AUTH_URL()}/auth/tokens",
            json=payload,
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        self._admin_token = r.headers["X-Subject-Token"]
        self._catalog_from_response(r.json())
        logger.info("Token admin obtenido correctamente")
        return self._admin_token

    def get_scoped_token(self, project_id: str) -> str:
        """Obtiene token scoped para un proyecto específico."""
        payload = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": OS_USERNAME(),
                            "password": OS_PASSWORD(),
                            "domain": {"name": OS_USER_DOMAIN_NAME()},
                        }
                    },
                },
                "scope": {"project": {"id": project_id}},
            }
        }

        r = httpx.post(
            f"{OS_AUTH_URL()}/auth/tokens",
            json=payload,
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        self._scoped_token = r.headers["X-Subject-Token"]
        self._project_id = project_id
        logger.info("Token scoped obtenido para proyecto %s", project_id)
        return self._scoped_token

    def _catalog_from_response(self, data: dict):
        """Extrae endpoints del catálogo de servicios."""
        catalog = data.get("token", {}).get("catalog", [])
        for service in catalog:
            stype = service.get("type")
            for endpoint in service.get("endpoints", []):
                if endpoint.get("interface") == "public":
                    self._endpoints[stype] = endpoint["url"]

    def _headers(self, token: str) -> dict:
        return {
            "X-Auth-Token": token,
            "Content-Type": "application/json",
        }

    # ── Endpoints ─────────────────────────────────────────────

    def keystone_url(self) -> str:
        return self._endpoints.get("identity", OS_AUTH_URL())

    def nova_url(self) -> str:
        return self._endpoints.get("compute", f"http://controller:8774/v2.1")

    def neutron_url(self) -> str:
        return self._endpoints.get("network", f"http://controller:9696")

    def glance_url(self) -> str:
        return self._endpoints.get("image", f"http://controller:9292")

    # ── Keystone CRUD ──────────────────────────────────────────

    def create_project(self, name: str, domain_id: str, token: str) -> dict:
        r = httpx.post(
            f"{self.keystone_url()}/projects",
            json={"project": {"name": name, "domain_id": domain_id, "enabled": True}},
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["project"]

    def get_project_by_name(self, name: str, domain_id: str, token: str) -> Optional[dict]:
        r = httpx.get(
            f"{self.keystone_url()}/projects",
            params={"name": name, "domain_id": domain_id},
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        projects = r.json().get("projects", [])
        return projects[0] if projects else None

    def delete_project(self, project_id: str, token: str):
        r = httpx.delete(
            f"{self.keystone_url()}/projects/{project_id}",
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        if r.status_code not in (200, 204, 404):
            r.raise_for_status()

    def get_user_by_name(self, name: str, domain_id: str, token: str) -> Optional[dict]:
        r = httpx.get(
            f"{self.keystone_url()}/users",
            params={"name": name, "domain_id": domain_id},
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        users = r.json().get("users", [])
        return users[0] if users else None

    def get_role_by_name(self, name: str, token: str) -> Optional[dict]:
        r = httpx.get(
            f"{self.keystone_url()}/roles",
            params={"name": name},
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        roles = r.json().get("roles", [])
        return roles[0] if roles else None

    def assign_role(self, project_id: str, user_id: str, role_id: str, token: str):
        r = httpx.put(
            f"{self.keystone_url()}/projects/{project_id}/users/{user_id}/roles/{role_id}",
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        if r.status_code not in (200, 204):
            r.raise_for_status()

    # ── Glance ────────────────────────────────────────────────

    def get_image_by_name(self, name: str, token: str) -> Optional[dict]:
        r = httpx.get(
            f"{self.glance_url()}/v2/images",
            params={"name": name},
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        images = r.json().get("images", [])
        return images[0] if images else None

    # ── Neutron ───────────────────────────────────────────────

    def create_network(self, name: str, token: str, project_id: str,
                       vlan_id: Optional[int] = None) -> dict:
        network = {
            "name": name,
            "admin_state_up": True,
            "project_id": project_id,
        }
        # Red tipo VLAN provider (capa 2, sin túneles)
        if vlan_id:
            network["provider:network_type"] = "vlan"
            network["provider:segmentation_id"] = vlan_id
            network["provider:physical_network"] = "physnet1"

        r = httpx.post(
            f"{self.neutron_url()}/v2.0/networks",
            json={"network": network},
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["network"]

    def create_subnet(self, name: str, network_id: str, cidr: str,
                      token: str, project_id: str,
                      enable_dhcp: bool = False) -> dict:
        r = httpx.post(
            f"{self.neutron_url()}/v2.0/subnets",
            json={
                "subnet": {
                    "name": name,
                    "network_id": network_id,
                    "ip_version": 4,
                    "cidr": cidr,
                    "enable_dhcp": enable_dhcp,
                    "project_id": project_id,
                }
            },
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["subnet"]

    def create_port(self, name: str, network_id: str, subnet_id: str,
                    token: str, project_id: str,
                    fixed_ip: Optional[str] = None) -> dict:
        port = {
            "name": name,
            "network_id": network_id,
            "admin_state_up": True,
            "project_id": project_id,
        }
        if fixed_ip:
            port["fixed_ips"] = [{"subnet_id": subnet_id, "ip_address": fixed_ip}]
        else:
            port["fixed_ips"] = [{"subnet_id": subnet_id}]

        r = httpx.post(
            f"{self.neutron_url()}/v2.0/ports",
            json={"port": port},
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["port"]

    def delete_port(self, port_id: str, token: str):
        r = httpx.delete(
            f"{self.neutron_url()}/v2.0/ports/{port_id}",
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        if r.status_code not in (200, 204, 404):
            r.raise_for_status()

    def delete_subnet(self, subnet_id: str, token: str):
        r = httpx.delete(
            f"{self.neutron_url()}/v2.0/subnets/{subnet_id}",
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        if r.status_code not in (200, 204, 404):
            r.raise_for_status()

    def delete_network(self, network_id: str, token: str):
        r = httpx.delete(
            f"{self.neutron_url()}/v2.0/networks/{network_id}",
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        if r.status_code not in (200, 204, 404):
            r.raise_for_status()

    def create_security_group(self, name: str, token: str, project_id: str) -> dict:
        r = httpx.post(
            f"{self.neutron_url()}/v2.0/security-groups",
            json={"security_group": {"name": name, "project_id": project_id}},
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["security_group"]

    def create_security_group_rule(self, sg_id: str, direction: str,
                                   protocol: Optional[str], token: str,
                                   port_min: Optional[int] = None,
                                   port_max: Optional[int] = None,
                                   remote_ip_prefix: str = "0.0.0.0/0"):
        rule = {
            "security_group_id": sg_id,
            "direction": direction,
            "ethertype": "IPv4",
            "remote_ip_prefix": remote_ip_prefix,
        }
        if protocol:
            rule["protocol"] = protocol
        if port_min is not None:
            rule["port_range_min"] = port_min
            rule["port_range_max"] = port_max

        r = httpx.post(
            f"{self.neutron_url()}/v2.0/security-group-rules",
            json={"security_group_rule": rule},
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        # 409 = regla ya existe
        if r.status_code not in (200, 201, 409):
            r.raise_for_status()

    def delete_security_group(self, sg_id: str, token: str):
        r = httpx.delete(
            f"{self.neutron_url()}/v2.0/security-groups/{sg_id}",
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        if r.status_code not in (200, 204, 404):
            r.raise_for_status()

    # ── Nova ──────────────────────────────────────────────────

    def get_flavor_id(self, name_or_id: str, token: str) -> str:
        """Busca flavor por nombre o devuelve el ID si ya es un ID."""
        r = httpx.get(
            f"{self.nova_url()}/flavors",
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        for f in r.json().get("flavors", []):
            if f["name"] == name_or_id or f["id"] == name_or_id:
                return f["id"]
        return name_or_id  # fallback: asumir que es ID directo

    def create_server(self, name: str, image_id: str, flavor_id: str,
                      port_ids: list[str], token: str,
                      availability_zone: Optional[str] = None) -> dict:
        networks = [{"port": pid} for pid in port_ids]
        server = {
            "name": name,
            "imageRef": image_id,
            "flavorRef": flavor_id,
            "networks": networks,
        }
        if availability_zone:
            server["availability_zone"] = availability_zone

        r = httpx.post(
            f"{self.nova_url()}/servers",
            json={"server": server},
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["server"]

    def get_server(self, server_id: str, token: str) -> dict:
        r = httpx.get(
            f"{self.nova_url()}/servers/{server_id}",
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["server"]

    def wait_for_server_active(self, server_id: str, token: str) -> str:
        """Espera a que el servidor esté ACTIVE o ERROR. Devuelve el status final."""
        deadline = time.time() + VM_ACTIVE_TIMEOUT
        while time.time() < deadline:
            server = self.get_server(server_id, token)
            status = server["status"]
            if status == "ACTIVE":
                return "ACTIVE"
            if status == "ERROR":
                fault = server.get("fault", {})
                raise RuntimeError(
                    f"VM {server_id} entró en ERROR: {fault.get('message', 'desconocido')}"
                )
            time.sleep(VM_POLL_INTERVAL)
        return "TIMEOUT"

    def delete_server(self, server_id: str, token: str):
        r = httpx.delete(
            f"{self.nova_url()}/servers/{server_id}",
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        if r.status_code not in (200, 202, 204, 404):
            r.raise_for_status()

    def server_action(self, server_id: str, action: str, token: str) -> dict:
        action_map = {
            "start": {"os-start": None},
            "stop": {"os-stop": None},
            "reboot": {"reboot": {"type": "SOFT"}},
            "pause": {"pause": None},
            "resume": {"unpause": None},
        }
        body = action_map.get(action)
        if body is None:
            raise ValueError(f"Acción no soportada: {action}")

        r = httpx.post(
            f"{self.nova_url()}/servers/{server_id}/action",
            json=body,
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        if r.status_code not in (200, 202):
            r.raise_for_status()
        return {"action": action, "server_id": server_id}

    def get_console_url(self, server_id: str, token: str) -> Optional[str]:
        """Obtiene URL de consola noVNC."""
        r = httpx.post(
            f"{self.nova_url()}/servers/{server_id}/remote-consoles",
            json={"remote_console": {"protocol": "vnc", "type": "novnc"}},
            headers={
                **self._headers(token),
                "OpenStack-API-Version": "compute 2.6",
            },
            timeout=TIMEOUT,
        )
        if r.status_code in (200, 201):
            return r.json().get("remote_console", {}).get("url")
        return None

    def list_servers_by_project(self, project_id: str, token: str) -> list[dict]:
        r = httpx.get(
            f"{self.nova_url()}/servers/detail",
            params={"project_id": project_id},
            headers=self._headers(token),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("servers", [])


# ─────────────────────────────────────────────────────────────
# OpenStack Driver — interfaz idéntica a LinuxDriver
# ─────────────────────────────────────────────────────────────

class OpenStackDriver:
    """
    Driver de OpenStack para el PUCP Private Cloud Orchestrator.

    Métodos públicos (misma interfaz que LinuxDriver):
      - create_graph_slice(request: dict) -> dict
      - delete_graph_slice(slice_id, vms, nat, dhcp) -> dict
      - action_graph_vm(vm, action) -> dict
    """

    def __init__(self):
        self.client = OpenStackClient()

    # ── Helpers internos ──────────────────────────────────────

    def _get_image_id(self, image_name: str, token: str) -> str:
        """Busca imagen por nombre en Glance."""
        # Intentar primero con el nombre exacto
        image = self.client.get_image_by_name(image_name, token)
        if image:
            return image["id"]
        # Fallback: buscar cirros si es imagen base
        image = self.client.get_image_by_name("cirros", token)
        if image:
            logger.warning(
                "Imagen %r no encontrada, usando cirros como fallback", image_name
            )
            return image["id"]
        raise RuntimeError(f"Imagen {image_name!r} no encontrada en Glance")

    def _setup_project(self, slice_id: str, admin_token: str) -> tuple[str, str]:
        """
        Crea o reutiliza el proyecto OpenStack para el slice.
        Asocia userG6 con rol member y cloud_admin con rol admin.
        Devuelve (project_id, scoped_token).
        """
        # Crear o buscar proyecto
        existing = self.client.get_project_by_name(
            slice_id, OS_DOMAIN_ID(), admin_token
        )
        if existing:
            project_id = existing["id"]
            logger.info("Proyecto %r ya existe: %s", slice_id, project_id)
        else:
            project = self.client.create_project(slice_id, OS_DOMAIN_ID(), admin_token)
            project_id = project["id"]
            logger.info("Proyecto %r creado: %s", slice_id, project_id)

        # Asignar rol member a userG6
        user = self.client.get_user_by_name(OS_SLICE_USER(), OS_DOMAIN_ID(), admin_token)
        role_member = self.client.get_role_by_name("member", admin_token)
        if user and role_member:
            self.client.assign_role(project_id, user["id"], role_member["id"], admin_token)
            logger.info("Rol member asignado a %s en proyecto %s", OS_SLICE_USER(), slice_id)

        # Asignar rol admin a cloud_admin para poder obtener scoped token
        admin_user = self.client.get_user_by_name(OS_USERNAME(), OS_DOMAIN_ID(), admin_token)
        role_admin = self.client.get_role_by_name("admin", admin_token)
        if admin_user and role_admin:
            self.client.assign_role(project_id, admin_user["id"], role_admin["id"], admin_token)
            logger.info("Rol admin asignado a %s en proyecto %s", OS_USERNAME(), slice_id)

        # Token scoped al proyecto
        scoped_token = self.client.get_scoped_token(project_id)
        return project_id, scoped_token

    def _create_security_group(self, slice_id: str, token: str,
                               project_id: str) -> str:
        """Crea security group con reglas básicas para el slice."""
        sg_name = f"sg-{slice_id}"
        try:
            sg = self.client.create_security_group(sg_name, token, project_id)
            sg_id = sg["id"]

            # Reglas: todo el tráfico interno + ICMP + SSH desde exterior
            self.client.create_security_group_rule(
                sg_id, "ingress", None, token  # todo ingress (misma red)
            )
            self.client.create_security_group_rule(
                sg_id, "egress", None, token  # todo egress
            )
            self.client.create_security_group_rule(
                sg_id, "ingress", "icmp", token  # ping
            )
            self.client.create_security_group_rule(
                sg_id, "ingress", "tcp", token, 22, 22  # SSH
            )
            logger.info("Security group %s creado", sg_name)
            return sg_id
        except Exception as exc:
            logger.warning("No se pudo crear security group: %s", exc)
            return ""

    # ── create_graph_slice ────────────────────────────────────

    def create_graph_slice(self, request: dict) -> dict:
        """
        Crea un slice completo en OpenStack.

        request = {
            slice_id, nodes, links, vlan_base,
            network_backend, internet_mode
        }

        Cada node tiene: name, image_name, vcpus, ram_mb, disk_gb,
                         server (worker asignado), vnc_port, internet
        """
        slice_id = request["slice_id"]
        nodes = request["nodes"]
        links = request["links"]
        vlan_base = request.get("vlan_base", 100)
        internet_mode = request.get("internet_mode", "none")

        logger.info("Creando slice OpenStack %r con %d VMs", slice_id, len(nodes))

        created_servers: list[dict] = []
        created_networks: list[dict] = []
        created_ports: list[dict] = []

        try:
            # 1. Auth
            admin_token = self.client.get_admin_token()
            project_id, scoped_token = self._setup_project(slice_id, admin_token)

            # 2. Imagen y flavor
            image_names = list({n["image_name"] for n in nodes})
            image_id = self._get_image_id(image_names[0], admin_token)
            flavor_id = OS_FLAVOR_ID()
            logger.info("Usando imagen %s, flavor %s", image_id, flavor_id)

            # 3. Security group
            sg_id = self._create_security_group(slice_id, scoped_token, project_id)

            # 4. Crear una red por link (topología capa 2)
            link_networks: dict[str, dict] = {}  # link_id → {network, subnet}
            for idx, link in enumerate(links):
                link_id = link.get("id") or link.get("from", f"link{idx}")
                vlan_id = vlan_base + idx
                net_name = f"net-{slice_id}-{link_id}"
                subnet_name = f"sub-{slice_id}-{link_id}"
                cidr = f"192.168.{100 + idx}.0/30"

                network = self.client.create_network(
                    net_name, scoped_token, project_id, vlan_id=vlan_id
                )
                subnet = self.client.create_subnet(
                    subnet_name, network["id"], cidr,
                    scoped_token, project_id, enable_dhcp=False
                )
                link_networks[link_id] = {
                    "network": network,
                    "subnet": subnet,
                    "vlan_id": vlan_id,
                    "cidr": cidr,
                }
                created_networks.append({
                    "network_id": network["id"],
                    "subnet_id": subnet["id"],
                    "token": scoped_token,
                })
                logger.info(
                    "Red %s (VLAN %d, %s) creada para link %s",
                    net_name, vlan_id, cidr, link_id
                )

            # 5. Determinar qué links conectan a cada VM
            node_links: dict[str, list[str]] = {n["name"]: [] for n in nodes}
            for link in links:
                link_id = link.get("id") or link.get("from", "link0")
                src = link.get("from") or link.get("node_a", "")
                dst = link.get("to") or link.get("node_b", "")
                if src in node_links:
                    node_links[src].append(link_id)
                if dst in node_links:
                    node_links[dst].append(link_id)

            # 6. Crear puertos y VMs
            vm_results: list[dict] = []

            for node in nodes:
                node_name = node["name"]
                port_ids: list[str] = []
                node_ifaces: list[dict] = []

                for link_id in node_links[node_name]:
                    net_info = link_networks.get(link_id)
                    if not net_info:
                        continue

                    port_name = f"port-{node_name}-{link_id}"
                    port = self.client.create_port(
                        port_name,
                        net_info["network"]["id"],
                        net_info["subnet"]["id"],
                        scoped_token,
                        project_id,
                    )
                    port_ids.append(port["id"])
                    created_ports.append({
                        "port_id": port["id"],
                        "token": scoped_token,
                    })

                    fixed_ip = port["fixed_ips"][0]["ip_address"] if port.get("fixed_ips") else None
                    node_ifaces.append({
                        "link_id": link_id,
                        "port_id": port["id"],
                        "mac_address": port["mac_address"],
                        "ip_address": fixed_ip,
                        "network_id": net_info["network"]["id"],
                        "vlan_id": net_info["vlan_id"],
                        "cidr": net_info["cidr"],
                    })

                logger.info(
                    "Creando VM %s con %d puertos en %s",
                    node_name, len(port_ids),
                    node.get("server", "auto")
                )

                # Zona de disponibilidad basada en el worker asignado por CP-SAT
                az = None

                server = self.client.create_server(
                    name=node_name,
                    image_id=image_id,
                    flavor_id=flavor_id,
                    port_ids=port_ids,
                    token=scoped_token,
                    availability_zone=az,
                )
                server_id = server["id"]

                # Esperar ACTIVE
                logger.info("Esperando ACTIVE para VM %s (%s)", node_name, server_id)
                final_status = self.client.wait_for_server_active(server_id, scoped_token)

                # Consola noVNC
                console_url = self.client.get_console_url(server_id, scoped_token)

                vm_result = {
                    "vm_id": node_name,
                    "name": node_name,
                    "server": node.get("server", ""),
                    "openstack_id": server_id,
                    "status": final_status.lower(),
                    "error": None if final_status == "ACTIVE" else f"Status: {final_status}",
                    "interfaces": node_ifaces,
                    "console_url": console_url,
                    "image_name": node.get("image_name", ""),
                    "vcpus": node.get("vcpus", 1),
                    "ram_mb": node.get("ram_mb", 512),
                    "disk_gb": node.get("disk_gb", 4),
                    "internet": node.get("internet", False),
                    "project_id": project_id,
                }
                vm_results.append(vm_result)
                created_servers.append({
                    "server_id": server_id,
                    "name": node_name,
                    "token": scoped_token,
                })

            # 7. Construir links de salida
            links_out = []
            for idx, link in enumerate(links):
                link_id = link.get("id") or link.get("from", f"link{idx}")
                net_info = link_networks.get(link_id, {})
                links_out.append({
                    "id": link_id,
                    "from": link.get("from") or link.get("node_a", ""),
                    "to": link.get("to") or link.get("node_b", ""),
                    "vlan_id": net_info.get("vlan_id"),
                    "network_id": net_info.get("network", {}).get("id"),
                    "cidr": net_info.get("cidr"),
                })

            logger.info("Slice OpenStack %r creado exitosamente", slice_id)

            return {
                "slice_id": slice_id,
                "success": True,
                "project_id": project_id,
                "vms": vm_results,
                "links": links_out,
                "nat": None,
                "dhcp": [],
                "nodes": nodes,
                "error": None,
                # Guardar para poder borrar después
                "_os_state": {
                    "project_id": project_id,
                    "admin_token": admin_token,
                    "scoped_token": scoped_token,
                    "sg_id": sg_id,
                    "networks": created_networks,
                },
            }

        except Exception as exc:
            logger.error("Error creando slice OpenStack %r: %s", slice_id, exc)
            # Rollback parcial
            self._rollback(
                created_servers, created_ports, created_networks, scoped_token
                if "scoped_token" in dir() else ""
            )
            raise RuntimeError(f"Error en OpenStack slice {slice_id}: {exc}") from exc

    # ── delete_graph_slice ────────────────────────────────────

    def delete_graph_slice(
        self,
        slice_id: str,
        vms: list[dict],
        nat: dict | None = None,
        dhcp: list[dict] | None = None,
    ) -> dict:
        """
        Elimina todas las VMs, puertos, redes y el proyecto del slice.
        """
        logger.info("Eliminando slice OpenStack %r (%d VMs)", slice_id, len(vms))
        success = True

        try:
            admin_token = self.client.get_admin_token()

            # Buscar proyecto del slice
            project = self.client.get_project_by_name(
                slice_id, OS_DOMAIN_ID(), admin_token
            )
            if not project:
                logger.warning("Proyecto %r no encontrado en OpenStack", slice_id)
                return {"slice_id": slice_id, "success": True, "note": "proyecto no encontrado"}

            project_id = project["id"]
            scoped_token = self.client.get_scoped_token(project_id)

            # 1. Borrar VMs
            for vm in vms:
                server_id = vm.get("openstack_id") or vm.get("vm_id")
                if not server_id:
                    continue
                try:
                    self.client.delete_server(server_id, scoped_token)
                    logger.info("VM %s eliminada", server_id)
                except Exception as exc:
                    logger.error("Error eliminando VM %s: %s", server_id, exc)
                    success = False

            # Esperar a que las VMs desaparezcan antes de borrar puertos/redes
            time.sleep(5)

            # 2. Borrar puertos
            try:
                r = httpx.get(
                    f"{self.client.neutron_url()}/v2.0/ports",
                    params={"project_id": project_id},
                    headers=self.client._headers(scoped_token),
                    timeout=TIMEOUT,
                )
                if r.status_code == 200:
                    for port in r.json().get("ports", []):
                        try:
                            self.client.delete_port(port["id"], scoped_token)
                        except Exception as exc:
                            logger.warning("Error borrando puerto %s: %s", port["id"], exc)
            except Exception as exc:
                logger.warning("Error listando puertos: %s", exc)

            # 3. Borrar subnets
            try:
                r = httpx.get(
                    f"{self.client.neutron_url()}/v2.0/subnets",
                    params={"project_id": project_id},
                    headers=self.client._headers(scoped_token),
                    timeout=TIMEOUT,
                )
                if r.status_code == 200:
                    for subnet in r.json().get("subnets", []):
                        try:
                            self.client.delete_subnet(subnet["id"], scoped_token)
                        except Exception as exc:
                            logger.warning("Error borrando subnet %s: %s", subnet["id"], exc)
            except Exception as exc:
                logger.warning("Error listando subnets: %s", exc)

            # 4. Borrar redes
            try:
                r = httpx.get(
                    f"{self.client.neutron_url()}/v2.0/networks",
                    params={"project_id": project_id},
                    headers=self.client._headers(scoped_token),
                    timeout=TIMEOUT,
                )
                if r.status_code == 200:
                    for net in r.json().get("networks", []):
                        try:
                            self.client.delete_network(net["id"], scoped_token)
                        except Exception as exc:
                            logger.warning("Error borrando red %s: %s", net["id"], exc)
            except Exception as exc:
                logger.warning("Error listando redes: %s", exc)

            # 5. Borrar security groups
            try:
                r = httpx.get(
                    f"{self.client.neutron_url()}/v2.0/security-groups",
                    params={"project_id": project_id},
                    headers=self.client._headers(scoped_token),
                    timeout=TIMEOUT,
                )
                if r.status_code == 200:
                    for sg in r.json().get("security_groups", []):
                        if sg.get("name") != "default":
                            try:
                                self.client.delete_security_group(sg["id"], scoped_token)
                            except Exception as exc:
                                logger.warning("Error borrando SG %s: %s", sg["id"], exc)
            except Exception as exc:
                logger.warning("Error listando security groups: %s", exc)

            # 6. Borrar proyecto
            try:
                self.client.delete_project(project_id, admin_token)
                logger.info("Proyecto %s eliminado", project_id)
            except Exception as exc:
                logger.error("Error eliminando proyecto %s: %s", project_id, exc)
                success = False

        except Exception as exc:
            logger.error("Error en delete_graph_slice %r: %s", slice_id, exc)
            success = False

        return {"slice_id": slice_id, "success": success}

    # ── action_graph_vm ───────────────────────────────────────

    def action_graph_vm(self, vm: dict, action: str) -> dict:
        """
        Ejecuta una acción sobre una VM de OpenStack.
        Acciones soportadas: start, stop, reboot, pause, resume
        """
        action = (action or "").strip().lower()
        server_id = vm.get("openstack_id") or vm.get("vm_id")
        if not server_id:
            raise ValueError("VM sin openstack_id")

        project_id = vm.get("project_id")
        if not project_id:
            raise ValueError("VM sin project_id")

        admin_token = self.client.get_admin_token()
        scoped_token = self.client.get_scoped_token(project_id)

        result = self.client.server_action(server_id, action, scoped_token)
        time.sleep(2)
        server = self.client.get_server(server_id, scoped_token)

        return {
            "vm_name": vm.get("name", server_id),
            "openstack_id": server_id,
            "action": action,
            "status": server.get("status", "unknown").lower(),
        }

    # ── Rollback ──────────────────────────────────────────────

    def _rollback(
        self,
        servers: list[dict],
        ports: list[dict],
        networks: list[dict],
        token: str,
    ):
        """Limpieza en caso de error durante create_graph_slice."""
        logger.warning("Iniciando rollback OpenStack...")

        for s in reversed(servers):
            try:
                self.client.delete_server(s["server_id"], s.get("token", token))
            except Exception as exc:
                logger.error("Rollback VM %s: %s", s["server_id"], exc)

        time.sleep(3)

        for p in reversed(ports):
            try:
                self.client.delete_port(p["port_id"], p.get("token", token))
            except Exception as exc:
                logger.error("Rollback port %s: %s", p["port_id"], exc)

        for n in reversed(networks):
            try:
                if n.get("subnet_id"):
                    self.client.delete_subnet(n["subnet_id"], n.get("token", token))
                self.client.delete_network(n["network_id"], n.get("token", token))
            except Exception as exc:
                logger.error("Rollback network %s: %s", n.get("network_id"), exc)