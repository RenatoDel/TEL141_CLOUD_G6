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
  7. Nova      → crear instancias con los puertos asignados (round-robin entre compute nodes)
  8. Nova      → obtener URL de consola noVNC (con fallback a endpoint legacy)

Mejoras de esta versión:
  - Helper único de HTTP (_request) que loggea el body completo en cada error.
  - create_port idempotente: ante 409 reutiliza el puerto existente por nombre.
  - OS_PHYSNET configurable (Kolla puede usar physnet0/physnet1/etc).
  - Idempotencia completa: proyecto/redes/subnets/puertos/VMs se reutilizan si existen.
  - Logging estructurado con prefijo [slice_id] en cada paso.
  - _rollback espera (polling) a que las VMs queden DELETED antes de borrar puertos.
  - wait_for_server_active renueva el token scoped si el polling supera 60s.
  - get_console_url cae a os-getVNCConsole (legacy) si remote-consoles da 404.
  - Placement real: round-robin entre OS_COMPUTE_NODES usando AZ "nova:<compute_node>".
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

# Red física del provider VLAN. En instalaciones Kolla puede ser physnet0.
def OS_PHYSNET(): return os.environ.get("OS_PHYSNET", "physnet1")

def OS_EXTERNAL_NETWORK_NAME(): return os.environ.get("OS_EXTERNAL_NETWORK_NAME", "external")


# Compute nodes reales de OpenStack (verificados con `openstack compute service list`).
# Si está vacío → Nova elige automáticamente (comportamiento anterior).
def OS_COMPUTE_NODES() -> list[str]:
    raw = os.environ.get("OS_COMPUTE_NODES", "").strip()
    if not raw:
        return []
    return [n.strip() for n in raw.split(",") if n.strip()]

TIMEOUT = 60.0
VM_ACTIVE_TIMEOUT = 120  # segundos máximos esperando ACTIVE
VM_POLL_INTERVAL = 3
TOKEN_RENEW_AFTER = 60   # renovar token scoped si el polling supera estos segundos
VM_DELETE_TIMEOUT = 60   # segundos máximos esperando a que las VMs queden DELETED


# ─────────────────────────────────────────────────────────────
# Logging estructurado: prefijo [slice_id] en cada mensaje
# ─────────────────────────────────────────────────────────────

class SliceLogAdapter(logging.LoggerAdapter):
    """Antepone [slice_id] a todos los logs del slice."""
    def process(self, msg, kwargs):
        return f"[{self.extra['slice_id']}] {msg}", kwargs


# ─────────────────────────────────────────────────────────────
# Cliente HTTP base
# ─────────────────────────────────────────────────────────────

class OpenStackClient:
    """
    Cliente HTTP liviano para las APIs de OpenStack.
    Maneja autenticación, tokens y logging granular de errores.
    """

    def __init__(self):
        self._admin_token: Optional[str] = None
        self._scoped_token: Optional[str] = None
        self._project_id: Optional[str] = None
        self._endpoints: dict[str, str] = {}

    # ── Helper HTTP central ───────────────────────────────────

    @staticmethod
    def _safe_body(r: httpx.Response) -> str:
        """Devuelve el body de la respuesta de forma segura (truncado)."""
        try:
            text = r.text
        except Exception:
            return "<sin body legible>"
        return text[:2000] if text else "<body vacío>"

    def _request(
        self,
        method: str,
        url: str,
        *,
        token: Optional[str] = None,
        headers: Optional[dict] = None,
        ok: tuple[int, ...] = (200, 201, 202, 204),
        raise_for_status: bool = True,
        **kwargs,
    ) -> httpx.Response:
        """
        Wrapper único para todas las llamadas HTTP.
        Loggea el body COMPLETO del error (no solo el status) cuando el
        código no está en `ok`. Devuelve siempre la respuesta para que el
        caller pueda inspeccionarla (ej. 409 en create_port).
        """
        if headers is None:
            headers = self._headers(token) if token else {}
        try:
            r = httpx.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
        except httpx.RequestError as exc:
            logger.error("Fallo de conexión %s %s: %s", method, url, exc)
            raise

        if r.status_code not in ok:
            logger.error(
                "%s %s → HTTP %d | body: %s",
                method, url, r.status_code, self._safe_body(r),
            )
            if raise_for_status:
                r.raise_for_status()
        return r

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

        r = self._request(
            "POST", f"{OS_AUTH_URL()}/auth/tokens",
            json=payload, ok=(200, 201),
        )
        self._admin_token = r.headers["X-Subject-Token"]
        self._catalog_from_response(r.json())
        logger.info("Token admin obtenido correctamente")
        return self._admin_token

    def get_scoped_token(self, project_id: str) -> str:
        """Obtiene token scoped para un proyecto específico (password auth directo)."""
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

        r = self._request(
            "POST", f"{OS_AUTH_URL()}/auth/tokens",
            json=payload, ok=(200, 201),
        )
        self._scoped_token = r.headers["X-Subject-Token"]
        self._project_id = project_id
        # El catálogo del token scoped tiene los endpoints del proyecto.
        self._catalog_from_response(r.json())
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
        return self._endpoints.get("compute", "http://controller:8774/v2.1")

    def neutron_url(self) -> str:
        return self._endpoints.get("network", "http://controller:9696")

    def glance_url(self) -> str:
        return self._endpoints.get("image", "http://controller:9292")

    # ── Keystone CRUD ──────────────────────────────────────────

    def create_project(self, name: str, domain_id: str, token: str) -> dict:
        r = self._request(
            "POST", f"{self.keystone_url()}/projects",
            token=token, ok=(200, 201),
            json={"project": {"name": name, "domain_id": domain_id, "enabled": True}},
        )
        return r.json()["project"]

    def get_project_by_name(self, name: str, domain_id: str, token: str) -> Optional[dict]:
        r = self._request(
            "GET", f"{self.keystone_url()}/projects",
            token=token, ok=(200,),
            params={"name": name, "domain_id": domain_id},
        )
        projects = r.json().get("projects", [])
        return projects[0] if projects else None

    def delete_project(self, project_id: str, token: str):
        self._request(
            "DELETE", f"{self.keystone_url()}/projects/{project_id}",
            token=token, ok=(200, 204, 404),
        )

    def get_user_by_name(self, name: str, domain_id: str, token: str) -> Optional[dict]:
        r = self._request(
            "GET", f"{self.keystone_url()}/users",
            token=token, ok=(200,),
            params={"name": name, "domain_id": domain_id},
        )
        users = r.json().get("users", [])
        return users[0] if users else None

    def get_role_by_name(self, name: str, token: str) -> Optional[dict]:
        r = self._request(
            "GET", f"{self.keystone_url()}/roles",
            token=token, ok=(200,),
            params={"name": name},
        )
        roles = r.json().get("roles", [])
        return roles[0] if roles else None

    def assign_role(self, project_id: str, user_id: str, role_id: str, token: str):
        self._request(
            "PUT",
            f"{self.keystone_url()}/projects/{project_id}/users/{user_id}/roles/{role_id}",
            token=token, ok=(200, 204),
        )

    # ── Glance ────────────────────────────────────────────────

    def get_image_by_name(self, name: str, token: str) -> Optional[dict]:
        r = self._request(
            "GET", f"{self.glance_url()}/v2/images",
            token=token, ok=(200,),
            params={"name": name},
        )
        images = r.json().get("images", [])
        return images[0] if images else None

    # ── Neutron ───────────────────────────────────────────────

    def get_network_by_name(self, name: str, project_id: str, token: str) -> Optional[dict]:
        r = self._request(
            "GET", f"{self.neutron_url()}/v2.0/networks",
            token=token, ok=(200,),
            params={"name": name, "project_id": project_id},
        )
        nets = r.json().get("networks", [])
        return nets[0] if nets else None
    
    def get_network_by_name_global(self, name: str, token: str) -> Optional[dict]:
        """
        Busca una red por nombre sin filtrar por project_id. Necesario para
        redes compartidas (shared=True) como la red 'external', que pertenece
        al proyecto admin pero debe ser usable desde cualquier slice/proyecto.
        """
        r = self._request(
            "GET", f"{self.neutron_url()}/v2.0/networks",
            token=token, ok=(200,),
            params={"name": name},
        )
        nets = r.json().get("networks", [])
        return nets[0] if nets else None

    def get_subnet_by_name(self, name: str, project_id: str, token: str) -> Optional[dict]:
        r = self._request(
            "GET", f"{self.neutron_url()}/v2.0/subnets",
            token=token, ok=(200,),
            params={"name": name, "project_id": project_id},
        )
        subnets = r.json().get("subnets", [])
        return subnets[0] if subnets else None

    def get_port_by_name(self, name: str, project_id: str, token: str) -> Optional[dict]:
        r = self._request(
            "GET", f"{self.neutron_url()}/v2.0/ports",
            token=token, ok=(200,),
            params={"name": name, "project_id": project_id},
        )
        ports = r.json().get("ports", [])
        return ports[0] if ports else None

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
            network["provider:physical_network"] = OS_PHYSNET()

        r = self._request(
            "POST", f"{self.neutron_url()}/v2.0/networks",
            token=token, ok=(200, 201),
            json={"network": network},
        )
        return r.json()["network"]

    def create_subnet(self, name: str, network_id: str, cidr: str,
                      token: str, project_id: str,
                      enable_dhcp: bool = False,
                      disable_gateway: bool = True) -> dict:
        subnet = {
            "name": name,
            "network_id": network_id,
            "ip_version": 4,
            "cidr": cidr,
            "enable_dhcp": enable_dhcp,
            "project_id": project_id,
            "gateway_ip": None, 
        }
        # Segmento L2 aislado (link punto a punto, sin router): el gateway no se
        # usa y, con uno por defecto, Neutron reserva la 1ra IP → el /30 queda
        # con una sola IP asignable y el 2do puerto falla con 409. Desactivado,
        # el /30 deja .1 y .2 = los 2 extremos del link.
        if disable_gateway:
            subnet["gateway_ip"] = None  # serializa a null → sin gateway
        r = self._request(
            "POST", f"{self.neutron_url()}/v2.0/subnets",
            token=token, ok=(200, 201),
            json={"subnet": subnet},
            
        )
        return r.json()["subnet"]

    def create_port(self, name: str, network_id: str, subnet_id: str,
                    token: str, project_id: str,
                    fixed_ip: Optional[str] = None) -> dict:
        """
        Crea un puerto. Idempotente: si ya existe uno con el mismo nombre se
        reutiliza. Ante un 409 (conflicto típico con subnet sin DHCP en redes
        VLAN provider) se busca y reutiliza el puerto existente en vez de fallar.
        """
        existing = self.get_port_by_name(name, project_id, token)
        if existing:
            logger.info("Puerto %s ya existe, reutilizando %s", name, existing["id"])
            return existing

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

        r = self._request(
            "POST", f"{self.neutron_url()}/v2.0/ports",
            token=token, ok=(200, 201, 409), raise_for_status=False,
            json={"port": port},
        )
        if r.status_code == 409:
            logger.warning("409 al crear puerto %s, buscando existente para reutilizar", name)
            existing = self.get_port_by_name(name, project_id, token)
            if existing:
                logger.info("Puerto %s reutilizado tras 409: %s", name, existing["id"])
                return existing
            # No se encontró: ahora sí propagar el error con el body ya logueado.
            r.raise_for_status()
        return r.json()["port"]

    def delete_port(self, port_id: str, token: str):
        self._request(
            "DELETE", f"{self.neutron_url()}/v2.0/ports/{port_id}",
            token=token, ok=(200, 204, 404),
        )

    def delete_subnet(self, subnet_id: str, token: str):
        self._request(
            "DELETE", f"{self.neutron_url()}/v2.0/subnets/{subnet_id}",
            token=token, ok=(200, 204, 404),
        )

    def delete_network(self, network_id: str, token: str):
        self._request(
            "DELETE", f"{self.neutron_url()}/v2.0/networks/{network_id}",
            token=token, ok=(200, 204, 404),
        )

    def get_security_group_by_name(self, name: str, project_id: str, token: str) -> Optional[dict]:
        r = self._request(
            "GET", f"{self.neutron_url()}/v2.0/security-groups",
            token=token, ok=(200,),
            params={"name": name, "project_id": project_id},
        )
        sgs = r.json().get("security_groups", [])
        return sgs[0] if sgs else None

    def create_security_group(self, name: str, token: str, project_id: str) -> dict:
        r = self._request(
            "POST", f"{self.neutron_url()}/v2.0/security-groups",
            token=token, ok=(200, 201),
            json={"security_group": {"name": name, "project_id": project_id}},
        )
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

        # 409 = regla ya existe (idempotente)
        self._request(
            "POST", f"{self.neutron_url()}/v2.0/security-group-rules",
            token=token, ok=(200, 201, 409), raise_for_status=False,
            json={"security_group_rule": rule},
        )

    def delete_security_group(self, sg_id: str, token: str):
        self._request(
            "DELETE", f"{self.neutron_url()}/v2.0/security-groups/{sg_id}",
            token=token, ok=(200, 204, 404),
        )

    # ── Nova ──────────────────────────────────────────────────

    def get_flavor_id(self, name_or_id: str, token: str) -> str:
        """Busca flavor por nombre o devuelve el ID si ya es un ID."""
        r = self._request(
            "GET", f"{self.nova_url()}/flavors",
            token=token, ok=(200,),
        )
        for f in r.json().get("flavors", []):
            if f["name"] == name_or_id or f["id"] == name_or_id:
                return f["id"]
        return name_or_id  # fallback: asumir que es ID directo

    def get_server_by_name(self, name: str, project_id: str, token: str) -> Optional[dict]:
        """Busca una VM por nombre exacto dentro del proyecto (para idempotencia)."""
        r = self._request(
            "GET", f"{self.nova_url()}/servers",
            token=token, ok=(200,),
            params={"name": name, "project_id": project_id},
        )
        servers = r.json().get("servers", [])
        # El filtro `name` de Nova es regex; quedarnos solo con el match exacto.
        for s in servers:
            if s.get("name") == name:
                return s
        return None

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

        r = self._request(
            "POST", f"{self.nova_url()}/servers",
            token=token, ok=(200, 201, 202),
            json={"server": server},
        )
        return r.json()["server"]

    def get_server(self, server_id: str, token: str) -> dict:
        r = self._request(
            "GET", f"{self.nova_url()}/servers/{server_id}",
            token=token, ok=(200,),
        )
        return r.json()["server"]

    def wait_for_server_active(self, server_id: str, token: str,
                               project_id: Optional[str] = None) -> str:
        """
        Espera a que el servidor esté ACTIVE o ERROR. Devuelve el status final.
        Si el polling supera TOKEN_RENEW_AFTER segundos, renueva el token scoped
        (requiere project_id; si no se pasa usa el último project_id conocido).
        """
        deadline = time.time() + VM_ACTIVE_TIMEOUT
        current_token = token
        last_renew = time.time()
        renew_project = project_id or self._project_id

        while time.time() < deadline:
            # Renovación proactiva del token si el polling se alarga.
            if renew_project and (time.time() - last_renew) > TOKEN_RENEW_AFTER:
                try:
                    current_token = self.get_scoped_token(renew_project)
                    last_renew = time.time()
                    logger.info("Token scoped renovado durante espera de VM %s", server_id)
                except Exception as exc:
                    logger.warning("No se pudo renovar token scoped: %s", exc)

            server = self.get_server(server_id, current_token)
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

    def wait_for_servers_deleted(self, server_ids: list[str], token: str,
                                 timeout: int = VM_DELETE_TIMEOUT) -> bool:
        """Espera (polling) a que las VMs dadas devuelvan 404. True si todas se borraron."""
        deadline = time.time() + timeout
        pending = {sid for sid in server_ids if sid}
        while pending and time.time() < deadline:
            for sid in list(pending):
                r = self._request(
                    "GET", f"{self.nova_url()}/servers/{sid}",
                    token=token, ok=(200, 404), raise_for_status=False,
                )
                if r.status_code == 404:
                    pending.discard(sid)
            if pending:
                time.sleep(VM_POLL_INTERVAL)
        if pending:
            logger.warning("Timeout esperando borrado de VMs: %s", pending)
        return not pending

    def delete_server(self, server_id: str, token: str):
        self._request(
            "DELETE", f"{self.nova_url()}/servers/{server_id}",
            token=token, ok=(200, 202, 204, 404),
        )

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

        self._request(
            "POST", f"{self.nova_url()}/servers/{server_id}/action",
            token=token, ok=(200, 202),
            json=body,
        )
        return {"action": action, "server_id": server_id}

    def get_console_url(self, server_id: str, token: str) -> Optional[str]:
        """
        Obtiene URL de consola noVNC.
        1) Endpoint moderno: POST /servers/{id}/remote-consoles (microversion 2.6).
        2) Fallback legacy:  POST /servers/{id}/action os-getVNCConsole (sin microversion).
        """
        r = self._request(
            "POST", f"{self.nova_url()}/servers/{server_id}/remote-consoles",
            headers={**self._headers(token), "OpenStack-API-Version": "compute 2.6"},
            ok=(200, 201, 400, 404), raise_for_status=False,
            json={"remote_console": {"protocol": "vnc", "type": "novnc"}},
        )
        if r.status_code in (200, 201):
            return r.json().get("remote_console", {}).get("url")

        logger.warning(
            "remote-consoles devolvió %d para %s, probando os-getVNCConsole (legacy)",
            r.status_code, server_id,
        )
        # El endpoint legacy NO debe enviar microversion 2.6 (fue removido en 2.6).
        r2 = self._request(
            "POST", f"{self.nova_url()}/servers/{server_id}/action",
            token=token, ok=(200, 201, 400, 404), raise_for_status=False,
            json={"os-getVNCConsole": {"type": "novnc"}},
        )
        if r2.status_code in (200, 201):
            return r2.json().get("console", {}).get("url")

        logger.warning("No se pudo obtener consola noVNC para %s", server_id)
        return None

    def list_servers_by_project(self, project_id: str, token: str) -> list[dict]:
        r = self._request(
            "GET", f"{self.nova_url()}/servers/detail",
            token=token, ok=(200,),
            params={"project_id": project_id},
        )
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

    def _pick_availability_zone(self, vm_index: int) -> Optional[str]:
        """
        Placement round-robin entre los compute nodes reales de OpenStack.

        El placement_service (CP-SAT) asigna workers con nombres del dominio
        Linux (server1/2/3) que NO existen en OpenStack, así que esa asignación
        se ignora aquí. En su lugar se distribuye balanceadamente entre los
        nodos de OS_COMPUTE_NODES usando el formato "nova:<compute_node>".

        Si OS_COMPUTE_NODES no está definido → None (Nova elige automáticamente).
        """
        nodes = OS_COMPUTE_NODES()
        if not nodes:
            return None
        compute_node = nodes[vm_index % len(nodes)]
        return f"nova:{compute_node}"

    def _setup_project(self, slice_id: str, admin_token: str,
                       slog: logging.LoggerAdapter) -> tuple[str, str]:
        """
        Crea o reutiliza el proyecto OpenStack para el slice.
        Asocia userG6 con rol member y cloud_admin con rol admin.
        Devuelve (project_id, scoped_token).
        """
        existing = self.client.get_project_by_name(
            slice_id, OS_DOMAIN_ID(), admin_token
        )
        if existing:
            project_id = existing["id"]
            slog.info("Proyecto %r ya existe: %s", slice_id, project_id)
        else:
            project = self.client.create_project(slice_id, OS_DOMAIN_ID(), admin_token)
            project_id = project["id"]
            slog.info("Proyecto %r creado: %s", slice_id, project_id)

        # Asignar rol member a userG6 (idempotente, PUT)
        user = self.client.get_user_by_name(OS_SLICE_USER(), OS_DOMAIN_ID(), admin_token)
        role_member = self.client.get_role_by_name("member", admin_token)
        if user and role_member:
            self.client.assign_role(project_id, user["id"], role_member["id"], admin_token)
            slog.info("Rol member asignado a %s", OS_SLICE_USER())

        # Asignar rol admin a cloud_admin para poder obtener scoped token
        admin_user = self.client.get_user_by_name(OS_USERNAME(), OS_DOMAIN_ID(), admin_token)
        role_admin = self.client.get_role_by_name("admin", admin_token)
        if admin_user and role_admin:
            self.client.assign_role(project_id, admin_user["id"], role_admin["id"], admin_token)
            slog.info("Rol admin asignado a %s", OS_USERNAME())

        scoped_token = self.client.get_scoped_token(project_id)
        return project_id, scoped_token

    def _create_security_group(self, slice_id: str, token: str,
                               project_id: str,
                               slog: logging.LoggerAdapter) -> str:
        """Crea (o reutiliza) el security group con reglas básicas para el slice."""
        sg_name = f"sg-{slice_id}"
        try:
            existing = self.client.get_security_group_by_name(sg_name, project_id, token)
            if existing:
                slog.info("Security group %s ya existe, reutilizando", sg_name)
                return existing["id"]

            sg = self.client.create_security_group(sg_name, token, project_id)
            sg_id = sg["id"]

            # Reglas: todo el tráfico interno + ICMP + SSH desde exterior
            self.client.create_security_group_rule(sg_id, "ingress", None, token)
            self.client.create_security_group_rule(sg_id, "egress", None, token)
            self.client.create_security_group_rule(sg_id, "ingress", "icmp", token)
            self.client.create_security_group_rule(sg_id, "ingress", "tcp", token, 22, 22)
            slog.info("Security group %s creado", sg_name)
            return sg_id
        except Exception as exc:
            slog.warning("No se pudo crear security group: %s", exc)
            return ""

    def _attach_internet_port(self, slice_id: str, node_name: str,
                              scoped_token: str, project_id: str,
                              slog: logging.LoggerAdapter) -> Optional[dict]:
        """
        Crea (o reutiliza) un puerto en la red 'external' (provider flat,
        creada en el Lab5/6) para dar salida/entrada a Internet a un nodo
        con internet=true. R5: acceso desde el exterior vía IP DHCP
        sobre 10.60.X.0/24.

        Devuelve el puerto creado o None si la red external no existe.
        """
        ext_network = self.client.get_network_by_name_global(
            OS_EXTERNAL_NETWORK_NAME(), scoped_token
        )
        if not ext_network:
            slog.warning(
                "Red externa %r no encontrada — el nodo %s no tendrá salida a Internet. "
                "Verifique que el Lab5/6 haya creado la red 'external'.",
                OS_EXTERNAL_NETWORK_NAME(), node_name,
            )
            return None

        port_name = f"port-ext-{slice_id}-{node_name}"
        existing = self.client.get_port_by_name(port_name, project_id, scoped_token)
        if existing:
            slog.info("Puerto externo %s ya existe, reutilizando", port_name)
            return existing

        port = {
            "name": port_name,
            "network_id": ext_network["id"],
            "admin_state_up": True,
            "project_id": project_id,
        }
        r = self.client._request(
            "POST", f"{self.client.neutron_url()}/v2.0/ports",
            token=scoped_token, ok=(200, 201, 409), raise_for_status=False,
            json={"port": port},
        )
        if r.status_code == 409:
            existing = self.client.get_port_by_name(port_name, project_id, scoped_token)
            if existing:
                slog.info("Puerto externo %s reutilizado tras 409", port_name)
                return existing
            r.raise_for_status()

        created = r.json()["port"]
        slog.info("Puerto externo %s creado para nodo %s", port_name, node_name)
        return created    

    # ── create_graph_slice ────────────────────────────────────

    def create_graph_slice(self, request: dict) -> dict:
        """
        Crea (o completa) un slice en OpenStack. Re-entrante / idempotente:
        proyecto, redes, subnets, puertos y VMs se reutilizan si ya existen.

        request = {
            slice_id, nodes, links, vlan_base,
            network_backend, internet_mode
        }
        """
        slice_id = request["slice_id"]
        nodes = request["nodes"]
        links = request["links"]
        vlan_base = request.get("vlan_base", 100)
        internet_mode = request.get("internet_mode", "none")

        slog = SliceLogAdapter(logger, {"slice_id": slice_id})
        slog.info("Creando slice OpenStack con %d VMs", len(nodes))

        # Solo se registran recursos NUEVOS para el rollback (no los reutilizados).
        created_servers: list[dict] = []
        created_networks: list[dict] = []
        created_ports: list[dict] = []
        scoped_token = ""  # garantizar definición para el rollback

        try:
            # 1. Auth + proyecto
            admin_token = self.client.get_admin_token()
            project_id, scoped_token = self._setup_project(slice_id, admin_token, slog)

            # 2. Imagen y flavor
            image_names = list({n["image_name"] for n in nodes})
            image_id = self._get_image_id(image_names[0], scoped_token)
            flavor_id = OS_FLAVOR_ID()
            slog.info("Usando imagen %s, flavor %s", image_id, flavor_id)

            # 3. Security group
            sg_id = self._create_security_group(slice_id, scoped_token, project_id, slog)

            # 4. Crear/reutilizar una red por link (topología capa 2)
            link_networks: dict[str, dict] = {}
            for idx, link in enumerate(links):
                link_id = link.get("id") or link.get("from", f"link{idx}")
                vlan_id = vlan_base + idx
                net_name = f"net-{slice_id}-{link_id}"
                subnet_name = f"sub-{slice_id}-{link_id}"
                cidr = f"192.168.{100 + idx}.0/30"

                network = self.client.get_network_by_name(net_name, project_id, scoped_token)
                if network:
                    slog.info("Red %s ya existe, reutilizando", net_name)
                else:
                    network = self.client.create_network(
                        net_name, scoped_token, project_id, vlan_id=vlan_id
                    )
                    created_networks.append({
                        "network_id": network["id"],
                        "subnet_id": None,  # se completa abajo
                        "token": scoped_token,
                    })
                    slog.info(
                        "Red %s (VLAN %d, %s, %s) creada para link %s",
                        net_name, vlan_id, cidr, OS_PHYSNET(), link_id
                    )

                subnet = self.client.get_subnet_by_name(subnet_name, project_id, scoped_token)
                if subnet:
                    slog.info("Subnet %s ya existe, reutilizando", subnet_name)
                else:
                    subnet = self.client.create_subnet(
                        subnet_name, network["id"], cidr,
                        scoped_token, project_id, enable_dhcp=False
                    )
                    # Vincular subnet a la red recién creada para el rollback
                    for cn in created_networks:
                        if cn["network_id"] == network["id"]:
                            cn["subnet_id"] = subnet["id"]
                            break

                link_networks[link_id] = {
                    "network": network,
                    "subnet": subnet,
                    "vlan_id": vlan_id,
                    "cidr": cidr,
                }

            # 5. Determinar qué links conectan a cada VM
            node_links: dict[str, list[str]] = {n["name"]: [] for n in nodes}
            for idx, link in enumerate(links):
                link_id = link.get("id") or link.get("from", f"link{idx}")
                src = link.get("from") or link.get("node_a", "")
                dst = link.get("to") or link.get("node_b", "")
                if src in node_links:
                    node_links[src].append(link_id)
                if dst in node_links:
                    node_links[dst].append(link_id)

            # 6. Crear/reutilizar puertos y VMs
            vm_results: list[dict] = []

            for vm_index, node in enumerate(nodes):
                node_name = node["name"]
                port_ids: list[str] = []
                node_ifaces: list[dict] = []

                for link_id in node_links[node_name]:
                    net_info = link_networks.get(link_id)
                    if not net_info:
                        continue

                    port_name = f"port-{node_name}-{link_id}"
                    existing_port = self.client.get_port_by_name(
                        port_name, project_id, scoped_token
                    )
                    if existing_port:
                        port = existing_port
                        slog.info("Puerto %s ya existe, reutilizando", port_name)
                    else:
                        port = self.client.create_port(
                            port_name,
                            net_info["network"]["id"],
                            net_info["subnet"]["id"],
                            scoped_token,
                            project_id,
                        )
                        created_ports.append({
                            "port_id": port["id"],
                            "token": scoped_token,
                        })

                    port_ids.append(port["id"])
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

                # R5 — salida/entrada a Internet: puerto extra en la red 'external'
                external_ip = None
                if node.get("internet"):
                    ext_port = self._attach_internet_port(
                        slice_id, node_name, scoped_token, project_id, slog
                    )
                    if ext_port:
                        port_ids.append(ext_port["id"])
                        external_ip = (
                            ext_port["fixed_ips"][0]["ip_address"]
                            if ext_port.get("fixed_ips") else None
                        )
                        created_ports.append({
                            "port_id": ext_port["id"],
                            "token": scoped_token,
                        })
                        node_ifaces.append({
                            "link_id": "external",
                            "port_id": ext_port["id"],
                            "mac_address": ext_port.get("mac_address"),
                            "ip_address": external_ip,
                            "network_id": ext_port.get("network_id"),
                            "vlan_id": None,
                            "cidr": None,
                        })

                # Placement: usar la asignación real del CP-SAT (placement_service)
                # Cada worker tiene su propia availability zone dedicada (az-worker1, az-worker2, az-worker3)
                worker = node.get("server", "")
                az = f"az-{worker}" if worker else None

                existing_server = self.client.get_server_by_name(
                    node_name, project_id, scoped_token
                )
                if existing_server:
                    server_id = existing_server["id"]
                    slog.info("VM %s ya existe (%s), reutilizando", node_name, server_id)
                else:
                    slog.info(
                        "Creando VM %s con %d puertos | AZ=%s",
                        node_name, len(port_ids), az or "auto"
                    )
                    server = self.client.create_server(
                        name=node_name,
                        image_id=image_id,
                        flavor_id=flavor_id,
                        port_ids=port_ids,
                        token=scoped_token,
                        availability_zone=az,
                    )
                    server_id = server["id"]
                    created_servers.append({
                        "server_id": server_id,
                        "name": node_name,
                        "token": scoped_token,
                    })

                # Esperar ACTIVE (con renovación de token si tarda)
                slog.info("Esperando ACTIVE para VM %s (%s)", node_name, server_id)
                final_status = self.client.wait_for_server_active(
                    server_id, scoped_token, project_id
                )

                # Consola noVNC (con fallback legacy)
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
                    "availability_zone": az,
                    "image_name": node.get("image_name", ""),
                    "vcpus": node.get("vcpus", 1),
                    "ram_mb": node.get("ram_mb", 512),
                    "disk_gb": node.get("disk_gb", 4),
                    "internet": node.get("internet", False),
                    "external_ip": external_ip,
                    "project_id": project_id,
                }
                vm_results.append(vm_result)

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

            slog.info("Slice OpenStack creado exitosamente")

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
                "_os_state": {
                    "project_id": project_id,
                    "admin_token": admin_token,
                    "scoped_token": scoped_token,
                    "sg_id": sg_id,
                    "networks": created_networks,
                },
            }

        except Exception as exc:
            slog.error("Error creando slice OpenStack: %s", exc)
            # Rollback parcial (solo recursos creados en esta corrida)
            self._rollback(created_servers, created_ports, created_networks, scoped_token, slog)
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
        slog = SliceLogAdapter(logger, {"slice_id": slice_id})
        slog.info("Eliminando slice OpenStack (%d VMs)", len(vms))
        success = True

        try:
            admin_token = self.client.get_admin_token()

            project = self.client.get_project_by_name(
                slice_id, OS_DOMAIN_ID(), admin_token
            )
            if not project:
                slog.warning("Proyecto no encontrado en OpenStack")
                return {"slice_id": slice_id, "success": True, "note": "proyecto no encontrado"}

            project_id = project["id"]
            scoped_token = self.client.get_scoped_token(project_id)

            # 1. Borrar VMs
            deleted_ids: list[str] = []
            for vm in vms:
                server_id = vm.get("openstack_id") or vm.get("vm_id")
                if not server_id:
                    continue
                try:
                    self.client.delete_server(server_id, scoped_token)
                    deleted_ids.append(server_id)
                    slog.info("VM %s solicitada para borrado", server_id)
                except Exception as exc:
                    slog.error("Error eliminando VM %s: %s", server_id, exc)
                    success = False

            # Esperar (polling) a que las VMs desaparezcan antes de borrar puertos/redes
            if deleted_ids:
                self.client.wait_for_servers_deleted(deleted_ids, scoped_token)

            # 2. Borrar puertos
            try:
                r = self.client._request(
                    "GET", f"{self.client.neutron_url()}/v2.0/ports",
                    token=scoped_token, ok=(200,), raise_for_status=False,
                    params={"project_id": project_id},
                )
                if r.status_code == 200:
                    for port in r.json().get("ports", []):
                        try:
                            self.client.delete_port(port["id"], scoped_token)
                        except Exception as exc:
                            slog.warning("Error borrando puerto %s: %s", port["id"], exc)
            except Exception as exc:
                slog.warning("Error listando puertos: %s", exc)

            # 3. Borrar subnets
            try:
                r = self.client._request(
                    "GET", f"{self.client.neutron_url()}/v2.0/subnets",
                    token=scoped_token, ok=(200,), raise_for_status=False,
                    params={"project_id": project_id},
                )
                if r.status_code == 200:
                    for subnet in r.json().get("subnets", []):
                        try:
                            self.client.delete_subnet(subnet["id"], scoped_token)
                        except Exception as exc:
                            slog.warning("Error borrando subnet %s: %s", subnet["id"], exc)
            except Exception as exc:
                slog.warning("Error listando subnets: %s", exc)

            # 4. Borrar redes
            try:
                r = self.client._request(
                    "GET", f"{self.client.neutron_url()}/v2.0/networks",
                    token=scoped_token, ok=(200,), raise_for_status=False,
                    params={"project_id": project_id},
                )
                if r.status_code == 200:
                    for net in r.json().get("networks", []):
                        try:
                            self.client.delete_network(net["id"], scoped_token)
                        except Exception as exc:
                            slog.warning("Error borrando red %s: %s", net["id"], exc)
            except Exception as exc:
                slog.warning("Error listando redes: %s", exc)

            # 5. Borrar security groups (menos el default)
            try:
                r = self.client._request(
                    "GET", f"{self.client.neutron_url()}/v2.0/security-groups",
                    token=scoped_token, ok=(200,), raise_for_status=False,
                    params={"project_id": project_id},
                )
                if r.status_code == 200:
                    for sg in r.json().get("security_groups", []):
                        if sg.get("name") != "default":
                            try:
                                self.client.delete_security_group(sg["id"], scoped_token)
                            except Exception as exc:
                                slog.warning("Error borrando SG %s: %s", sg["id"], exc)
            except Exception as exc:
                slog.warning("Error listando security groups: %s", exc)

            # 6. Borrar proyecto
            try:
                self.client.delete_project(project_id, admin_token)
                slog.info("Proyecto %s eliminado", project_id)
            except Exception as exc:
                slog.error("Error eliminando proyecto %s: %s", project_id, exc)
                success = False

        except Exception as exc:
            slog.error("Error en delete_graph_slice: %s", exc)
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
        slog: Optional[logging.LoggerAdapter] = None,
    ):
        """Limpieza en caso de error durante create_graph_slice."""
        log = slog or logger
        log.warning("Iniciando rollback OpenStack...")

        # 1. Solicitar borrado de VMs
        deleted_ids: list[str] = []
        for s in reversed(servers):
            try:
                self.client.delete_server(s["server_id"], s.get("token", token))
                deleted_ids.append(s["server_id"])
            except Exception as exc:
                log.error("Rollback VM %s: %s", s["server_id"], exc)

        # 2. Esperar (polling, hasta 60s) a que las VMs queden DELETED
        #    antes de tocar puertos/redes (un puerto en uso no se puede borrar).
        if deleted_ids and token:
            self.client.wait_for_servers_deleted(deleted_ids, token)

        # 3. Borrar puertos
        for p in reversed(ports):
            try:
                self.client.delete_port(p["port_id"], p.get("token", token))
            except Exception as exc:
                log.error("Rollback port %s: %s", p["port_id"], exc)

        # 4. Borrar subnets + redes
        for n in reversed(networks):
            try:
                if n.get("subnet_id"):
                    self.client.delete_subnet(n["subnet_id"], n.get("token", token))
                self.client.delete_network(n["network_id"], n.get("token", token))
            except Exception as exc:
                log.error("Rollback network %s: %s", n.get("network_id"), exc)