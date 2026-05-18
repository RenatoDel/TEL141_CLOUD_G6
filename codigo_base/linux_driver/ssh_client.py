from __future__ import annotations
"""
ssh_client.py
-------------
Cliente SSH para la topología VNRT del curso TEL141.

Conexión desde tu máquina local:
    ssh ubuntu@10.20.12.70 -p 580X  (X = número de servidor)

Conexión entre servidores (passwordless configurado en lab4):
    server4 (10.0.10.4) → server1/2/3 (10.0.10.X) via clave ECDSA

Autenticación:
    - Desde tu máquina: contraseña
    - Entre servidores: clave SSH passwordless
"""

import paramiko
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

COMMAND_TIMEOUT = 120
CONNECT_TIMEOUT = 30
MAX_RETRIES     = 3
RETRY_DELAY     = 5


class SSHError(Exception):
    pass

class SSHConnectionError(SSHError):
    pass

class SSHCommandError(SSHError):
    def __init__(self, command: str, exit_code: int, stderr: str):
        self.command   = command
        self.exit_code = exit_code
        self.stderr    = stderr
        super().__init__(
            f"Comando falló (exit={exit_code})\n"
            f"Comando : {command}\n"
            f"Stderr  : {stderr}"
        )

class SSHTimeoutError(SSHError):
    pass


class SSHClient:
    """
    Cliente SSH para conectarse a los servidores VNRT.

    Ejemplo — desde tu máquina via gateway:
        client = SSHClient(
            host="10.20.12.70", port=5801,
            user="ubuntu", password="tu_password"
        )

    Ejemplo — entre servidores (desde server4):
        client = SSHClient(
            host="10.0.10.1", port=22,
            user="ubuntu", key_path="/home/ubuntu/.ssh/id_ecdsa"
        )
    """

    def __init__(
        self,
        host:     str,
        user:     str = "ubuntu",
        port:     int = 22,
        password: Optional[str] = None,
        key_path: Optional[str] = None,
    ):
        self.host     = host
        self.user     = user
        self.port     = port
        self.password = password
        self.key_path = key_path
        self._client  = None

    def connect(self):
        """Abre la conexión SSH con reintentos automáticos."""
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(
                    f"[SSH] Conectando {self.user}@{self.host}:{self.port} "
                    f"(intento {attempt}/{MAX_RETRIES})"
                )
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

                kwargs = dict(
                    hostname=self.host,
                    port=self.port,
                    username=self.user,
                    timeout=CONNECT_TIMEOUT,
                )
                if self.key_path:
                    kwargs["key_filename"] = self.key_path
                elif self.password:
                    kwargs["password"] = self.password
                else:
                    raise SSHConnectionError("Necesitas password o key_path.")

                client.connect(**kwargs)
                self._client = client
                logger.info(f"[SSH] Conectado a {self.host}:{self.port}")
                return

            except paramiko.AuthenticationException as e:
                raise SSHConnectionError(f"Auth fallida en {self.host}: {e}") from e
            except Exception as e:
                last_error = e
                logger.warning(f"[SSH] Intento {attempt} fallido: {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)

        raise SSHConnectionError(
            f"No se pudo conectar a {self.host}:{self.port} "
            f"tras {MAX_RETRIES} intentos. Último error: {last_error}"
        )

    def disconnect(self):
        if self._client:
            try:
                self._client.close()
                logger.info(f"[SSH] Desconectado de {self.host}")
            except Exception:
                pass
            finally:
                self._client = None

    def is_connected(self) -> bool:
        if not self._client:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()

    def execute(
        self,
        command:        str,
        timeout:        int  = COMMAND_TIMEOUT,
        raise_on_error: bool = True,
    ) -> tuple[str, str]:
        """
        Ejecuta un comando en el servidor remoto.
        Retorna (stdout, stderr).
        """
        if not self.is_connected():
            raise SSHConnectionError(f"Sin conexión a {self.host}.")

        logger.debug(f"[SSH] {self.host} $ {command}")

        try:
            _, stdout_ch, stderr_ch = self._client.exec_command(command, timeout=timeout)
            out       = stdout_ch.read().decode("utf-8", errors="replace").strip()
            err       = stderr_ch.read().decode("utf-8", errors="replace").strip()
            exit_code = stdout_ch.channel.recv_exit_status()

            if out: logger.debug(f"[SSH] stdout: {out[:300]}")
            if err: logger.debug(f"[SSH] stderr: {err[:300]}")

            if raise_on_error and exit_code != 0:
                raise SSHCommandError(command, exit_code, err)

            return out, err

        except (SSHCommandError, SSHTimeoutError):
            raise
        except Exception as e:
            raise SSHError(f"Error ejecutando comando en {self.host}: {e}") from e

    def sudo(self, command: str, timeout: int = COMMAND_TIMEOUT, raise_on_error: bool = True) -> tuple[str, str]:
        """Ejecuta comando con sudo."""
        return self.execute(f"sudo {command}", timeout=timeout, raise_on_error=raise_on_error)

    def file_exists(self, path: str) -> bool:
        out, _ = self.execute(
            f"test -f {path} && echo yes || echo no",
            raise_on_error=False,
        )
        return out.strip() == "yes"

    def dir_exists(self, path: str) -> bool:
        out, _ = self.execute(
            f"test -d {path} && echo yes || echo no",
            raise_on_error=False,
        )
        return out.strip() == "yes"

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()
        return False


# ------------------------------------------------------------------
# Helpers para crear clientes según la topología VNRT
# ------------------------------------------------------------------

# Ajusta el gateway IP si cambia en tu grupo
GATEWAY_IP = "10.20.12.70"

SERVERS_VIA_GATEWAY = {
    "server1": 5801,
    "server2": 5802,
    "server3": 5803,  # headnode — networking, DHCP, iptables
    "server4": 5804,  # cliente — ejecuta scripts
}

SERVERS_INTERNAL = {
    "server1": "10.0.10.1",
    "server2": "10.0.10.2",
    "server3": "10.0.10.3",
    "server4": "10.0.10.4",
}


def get_client(server: str, password: str) -> "SSHClient":
    """
    Crea cliente SSH via gateway desde tu máquina local.

    Uso:
        with get_client("server1", "mi_password") as c:
            out, _ = c.execute("hostname")
    """
    port = SERVERS_VIA_GATEWAY[server]
    return SSHClient(
        host=GATEWAY_IP,
        port=port,
        user="ubuntu",
        password=password,
    )


def get_client_internal(
    server:   str,
    key_path: str = "/home/ubuntu/.ssh/id_ecdsa",
) -> "SSHClient":
    """
    Crea cliente SSH directo entre servidores (passwordless, desde server4).

    Uso (ejecutar estando en server4):
        with get_client_internal("server1") as c:
            out, _ = c.execute("hostname")
    """
    ip = SERVERS_INTERNAL[server]
    return SSHClient(
        host=ip,
        port=22,
        user="ubuntu",
        key_path=key_path,
    )
