from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
from pathlib import Path

import httpx
import paramiko
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SLICE_MANAGER_URL = os.getenv("SLICE_MANAGER_URL", "http://slice_manager:9002").rstrip("/")
AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL", "http://auth_service:9001").rstrip("/")
IMAGE_SERVICE_URL = os.getenv("IMAGE_SERVICE_URL", "http://image_service:9004").rstrip("/")

SSH_USER = os.getenv("WORKER_SSH_USER", "ubuntu")
SSH_KEY_PATH = os.getenv("WORKER_SSH_KEY_PATH", "/app/ssh_keys/id_ecdsa")
SSH_PASSWORD = os.getenv("WORKER_SSH_PASSWORD", "")
SSH_CONNECT_TIMEOUT = int(os.getenv("WORKER_SSH_TIMEOUT", "15"))

# WORKER_MAP: cada entrada apunta al gateway físico (10.20.11.189) en el puerto
# que el gateway redirige a ese worker.  Con la VPN del G6 activa y los túneles
# SSH levantados, el gateway físico hace el forwarding internamente:
#   gateway:5811 → server1 (192.168.201.1)
#   gateway:5812 → server2 (192.168.201.2)
#   gateway:5813 → server3 (192.168.201.3)
# Así el contenedor pucp_gateway solo necesita alcanzar 10.20.11.189, que SÍ
# es accesible desde app (10.20.11.113).
_GW_HOST = os.getenv("GATEWAY_PHYSICAL_HOST", "10.20.11.189")

WORKER_MAP = {
    "server1": {
        "host": os.getenv("WORKER_SERVER1_HOST", _GW_HOST),
        "port": int(os.getenv("WORKER_SERVER1_PORT", "5811")),
    },
    "server2": {
        "host": os.getenv("WORKER_SERVER2_HOST", _GW_HOST),
        "port": int(os.getenv("WORKER_SERVER2_PORT", "5812")),
    },
    "server3": {
        "host": os.getenv("WORKER_SERVER3_HOST", _GW_HOST),
        "port": int(os.getenv("WORKER_SERVER3_PORT", "5813")),
    },
}

app = FastAPI(title="PUCP Cloud Gateway", version="0.6.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


async def forward_request(base_url: str, path: str, request: Request) -> Response:
    url = f"{base_url}/{path}" if path else base_url
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            body = await request.body()
            response = await client.request(
                method=request.method,
                url=url,
                headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
                content=body,
                params=dict(request.query_params),
            )

        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return JSONResponse(status_code=response.status_code, content=response.json())

        return Response(
            content=response.content,
            status_code=response.status_code,
            media_type=content_type or None,
        )
    except httpx.ConnectError as exc:
        raise HTTPException(status_code=503, detail=f"Servicio no disponible: {base_url}") from exc
    except json.JSONDecodeError:
        return Response(content=response.text, status_code=response.status_code)
    except Exception as exc:
        logger.exception("Error en proxy")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _require_worker_and_token(worker_name: str, token: str):
    if not worker_name:
        raise RuntimeError("Falta worker")
    if worker_name not in WORKER_MAP:
        raise RuntimeError("Worker inválido")
    if not token:
        raise RuntimeError("Falta token")


def _build_ssh_client(worker_name: str) -> paramiko.SSHClient:
    worker = WORKER_MAP.get(worker_name)
    if not worker:
        raise RuntimeError(f"Worker no soportado: {worker_name}")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs = {
        "hostname": worker["host"],
        "port": worker["port"],
        "username": SSH_USER,
        "timeout": SSH_CONNECT_TIMEOUT,
        "banner_timeout": SSH_CONNECT_TIMEOUT,
        "auth_timeout": SSH_CONNECT_TIMEOUT,
    }

    if Path(SSH_KEY_PATH).exists():
        connect_kwargs["key_filename"] = SSH_KEY_PATH
    elif SSH_PASSWORD:
        connect_kwargs["password"] = SSH_PASSWORD
    else:
        raise RuntimeError(
            f"No se encontró llave SSH en {SSH_KEY_PATH} y tampoco WORKER_SSH_PASSWORD"
        )

    ssh.connect(**connect_kwargs)
    return ssh


def _open_ssh_to_worker(worker_name: str) -> tuple[paramiko.SSHClient, paramiko.Channel]:
    ssh = _build_ssh_client(worker_name)
    worker = WORKER_MAP[worker_name]

    chan = ssh.invoke_shell(term="xterm-256color", width=140, height=40)
    chan.settimeout(0.0)

    chan.send("export TERM=xterm-256color\n")
    chan.send("clear\n")
    chan.send(f'echo "[PUCP Cloud] Conectado a {worker_name} ({worker["host"]})"\n')
    chan.send("echo '[PUCP Cloud] Terminal del worker lista'\n")
    chan.send("uname -a || true\n")
    chan.send("pwd || true\n")

    return ssh, chan


def _open_vm_serial(worker_name: str, vm_name: str) -> tuple[paramiko.SSHClient, paramiko.Channel]:
    ssh = _build_ssh_client(worker_name)

    serial_path = f"/var/run/qemu-{vm_name}.serial"
    remote_py = r"""
import os, socket, select, sys

path = sys.argv[1]
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(path)
sock.setblocking(False)

stdin_fd = 0
stdout_fd = 1

while True:
    r, _, _ = select.select([sock, stdin_fd], [], [])
    if sock in r:
        data = sock.recv(4096)
        if not data:
            break
        os.write(stdout_fd, data)
    if stdin_fd in r:
        data = os.read(stdin_fd, 1024)
        if not data:
            break
        sock.sendall(data)
"""

    cmd = "sudo python3 -u -c {code} {path}".format(
        code=shlex.quote(remote_py),
        path=shlex.quote(serial_path),
    )

    chan = ssh.get_transport().open_session()
    chan.exec_command(cmd)
    chan.settimeout(0.0)
    return ssh, chan


def _read_channel(channel: paramiko.Channel) -> str:
    chunks: list[bytes] = []
    try:
        while channel.recv_ready():
            chunks.append(channel.recv(4096))
    except Exception:
        pass
    if not chunks:
        return ""
    return b"".join(chunks).decode(errors="ignore")


async def _bridge_channel_websocket(
    websocket: WebSocket,
    channel: paramiko.Channel,
):
    stop_event = asyncio.Event()

    async def pump_ssh_to_ws():
        while not stop_event.is_set():
            data = await asyncio.to_thread(_read_channel, channel)
            if data:
                await websocket.send_text(data)

            if channel.closed or channel.exit_status_ready():
                stop_event.set()
                break

            await asyncio.sleep(0.03)

    async def pump_ws_to_ssh():
        while not stop_event.is_set():
            try:
                message = await websocket.receive_text()
            except WebSocketDisconnect:
                stop_event.set()
                break

            if message.startswith("__resize__:"):
                try:
                    _, cols, rows = message.split(":")
                    await asyncio.to_thread(
                        channel.resize_pty,
                        width=max(40, int(cols)),
                        height=max(12, int(rows)),
                    )
                except Exception:
                    pass
                continue

            await asyncio.to_thread(channel.send, message)

    tasks = [
        asyncio.create_task(pump_ssh_to_ws()),
        asyncio.create_task(pump_ws_to_ssh()),
    ]

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    stop_event.set()

    for task in pending:
        task.cancel()

    for task in done:
        with contextlib.suppress(Exception):
            await task


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.api_route("/auth/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_auth(path: str, request: Request):
    return await forward_request(AUTH_SERVICE_URL, path, request)


@app.api_route("/api/images", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_images_root(request: Request):
    return await forward_request(IMAGE_SERVICE_URL, "images", request)


@app.put("/api/images/upload-raw")
async def proxy_images_upload_raw(request: Request):
    """
    Proxy especial para subir imágenes grandes.
    No usa await request.body(), porque eso carga todo el archivo en RAM.
    """
    url = f"{IMAGE_SERVICE_URL}/images/upload-raw"

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in {"host", "content-length"}
    }

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "PUT",
                url,
                headers=headers,
                params=dict(request.query_params),
                content=request.stream(),
            ) as resp:
                resp_body = await resp.aread()
                content_type = resp.headers.get("content-type", "")

        if "application/json" in content_type:
            try:
                return JSONResponse(
                    status_code=resp.status_code,
                    content=json.loads(resp_body),
                )
            except Exception:
                return Response(
                    content=resp_body,
                    status_code=resp.status_code,
                    media_type=content_type,
                )

        return Response(
            content=resp_body,
            status_code=resp.status_code,
            media_type=content_type or None,
        )

    except httpx.ConnectError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Servicio no disponible: {IMAGE_SERVICE_URL}",
        ) from exc
    except Exception as exc:
        logger.exception("Error en proxy upload-raw")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.api_route("/api/images/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_images(path: str, request: Request):
    return await forward_request(IMAGE_SERVICE_URL, f"images/{path}", request)


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_slice_manager(path: str, request: Request):
    return await forward_request(SLICE_MANAGER_URL, path, request)


@app.websocket("/ws/worker-shell")
async def ws_worker_shell(websocket: WebSocket):
    worker = websocket.query_params.get("worker", "").strip()
    token = websocket.query_params.get("token", "").strip()

    try:
        _require_worker_and_token(worker, token)
    except Exception as exc:
        await websocket.close(code=1008, reason=str(exc))
        return

    await websocket.accept()

    ssh = None
    channel = None
    try:
        ssh, channel = await asyncio.to_thread(_open_ssh_to_worker, worker)
        await _bridge_channel_websocket(websocket, channel)
    except Exception as exc:
        logger.exception("Error en terminal worker para %s", worker)
        with contextlib.suppress(Exception):
            await websocket.send_text(f"\r\n[PUCP Cloud] Error: {exc}\r\n")
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)
    finally:
        with contextlib.suppress(Exception):
            if channel is not None:
                channel.close()
        with contextlib.suppress(Exception):
            if ssh is not None:
                ssh.close()


@app.websocket("/ws/vm-serial")
async def ws_vm_serial(websocket: WebSocket):
    worker = websocket.query_params.get("worker", "").strip()
    vm = websocket.query_params.get("vm", "").strip()
    token = websocket.query_params.get("token", "").strip()

    try:
        _require_worker_and_token(worker, token)
        if not vm:
            raise RuntimeError("Falta vm")
    except Exception as exc:
        await websocket.close(code=1008, reason=str(exc))
        return

    await websocket.accept()

    ssh = None
    channel = None
    try:
        ssh, channel = await asyncio.to_thread(_open_vm_serial, worker, vm)
        await _bridge_channel_websocket(websocket, channel)
    except Exception as exc:
        logger.exception("Error en serial console de %s en %s", vm, worker)
        with contextlib.suppress(Exception):
            await websocket.send_text(f"\r\n[PUCP Cloud] Error serial VM: {exc}\r\n")
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)
    finally:
        with contextlib.suppress(Exception):
            if channel is not None:
                channel.close()
        with contextlib.suppress(Exception):
            if ssh is not None:
                ssh.close()


def _open_vnc_channel_sync(
    ssh_host: str,
    ssh_port: int,
    vnc_port: int,
) -> tuple[paramiko.SSHClient, paramiko.Channel]:
    """
    Abre un canal TCP hacia el puerto VNC de una VM en el worker.

    Topología real (Fase 2, Grupo 6):
        pucp_gateway (contenedor) → SSH → gateway físico 10.20.11.189:ssh_port
                                          (el gateway hace port-forward al workerN)
        → direct-tcpip → 127.0.0.1:vnc_port  (QEMU VNC en el worker)

    QEMU se lanza con:  -vnc :<display>   (sin password, sin TLS)
    Donde vnc_port = 5900 + display.

    Compatible con cirros (dropbear + QEMU mínimo) y Ubuntu (cloud-init).
    """
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs: dict = {
        "hostname": ssh_host,
        "port": ssh_port,
        "username": SSH_USER,
        "timeout": SSH_CONNECT_TIMEOUT,
        "banner_timeout": SSH_CONNECT_TIMEOUT,
        "auth_timeout": SSH_CONNECT_TIMEOUT,
        "look_for_keys": False,
        "allow_agent": False,
        # Deshabilitar compresión para no añadir latencia en el stream VNC
        "compress": False,
    }

    key_path = Path(SSH_KEY_PATH)
    if key_path.exists():
        # Intentar ECDSA primero (llave del proyecto); si falla, RSA genérico
        try:
            connect_kwargs["pkey"] = paramiko.ECDSAKey.from_private_key_file(str(key_path))
        except paramiko.SSHException:
            connect_kwargs["key_filename"] = str(key_path)
    elif SSH_PASSWORD:
        connect_kwargs["password"] = SSH_PASSWORD
    else:
        raise RuntimeError(
            f"No hay llave SSH en {SSH_KEY_PATH} ni WORKER_SSH_PASSWORD configurado"
        )

    logger.info("VNC: SSH → %s:%d (worker port-forward)", ssh_host, ssh_port)
    ssh.connect(**connect_kwargs)

    transport = ssh.get_transport()
    if transport is None:
        raise RuntimeError("Transport SSH nulo tras connect()")

    # Mantener alive el transporte — importante para streams VNC largos
    transport.set_keepalive(20)

    logger.info("VNC: abriendo direct-tcpip → 127.0.0.1:%d (QEMU VNC)", vnc_port)
    channel = transport.open_channel(
        "direct-tcpip",
        dest_addr=("127.0.0.1", vnc_port),
        src_addr=("127.0.0.1", 0),
        timeout=10,
    )

    # Sin timeout bloqueante en recv — usamos run_in_executor con canal sin timeout
    # para no mezclar el loop de asyncio con bloqueos de paramiko.
    channel.setblocking(True)
    channel.settimeout(None)   # bloqueante puro; asyncio lo corre en executor

    logger.info("VNC: canal direct-tcpip abierto OK hacia 127.0.0.1:%d", vnc_port)
    return ssh, channel


@app.websocket("/ws/vnc-proxy")
async def ws_vnc_proxy(websocket: WebSocket):
    """
    Proxy WebSocket ↔ VNC (RFB) para las VMs del cluster Linux.

    El flujo es:
      noVNC (browser) --WS--> /ws/vnc-proxy --SSH direct-tcpip--> QEMU VNC

    Parámetros query string:
      worker  : nombre del worker (server1 / server2 / server3)
      port    : puerto VNC de la VM (5900 + display, ej. 5901)
      token   : JWT del sistema (validación mínima: no vacío)
      vm      : nombre de la VM (solo para logging)

    Funciona con cirros (imagen ligera, QEMU sin password VNC) y Ubuntu
    (cloud-init, misma arquitectura de VNC sin password).
    """
    worker   = websocket.query_params.get("worker", "").strip()
    token    = websocket.query_params.get("token", "").strip()
    port_raw = websocket.query_params.get("port", "").strip()
    vm_name  = websocket.query_params.get("vm", "unknown").strip()

    # ── Validación temprana (antes de accept para poder cerrar con código) ──
    try:
        _require_worker_and_token(worker, token)
        vnc_port = int(port_raw)
        if not (5900 <= vnc_port <= 65535):
            raise ValueError(f"Puerto VNC fuera de rango: {vnc_port}")
    except Exception as exc:
        logger.warning("VNC rechazado: %s", exc)
        await websocket.close(code=1008, reason=str(exc))
        return

    # noVNC envía "Sec-WebSocket-Protocol: binary" en el handshake y REQUIERE
    # que el servidor lo confirme. Si el servidor no responde con ese subprotocolo,
    # noVNC cierra el WebSocket inmediatamente (antes de enviar o recibir datos RFB).
    # FastAPI/Starlette lo soporta pasando subprotocol= al accept().
    requested_protocols = websocket.headers.get("sec-websocket-protocol", "")
    subprotocol = "binary" if "binary" in requested_protocols else None
    await websocket.accept(subprotocol=subprotocol)
    logger.info(
        "VNC WebSocket aceptado: worker=%s vm=%s port=%d subprotocol=%s",
        worker, vm_name, vnc_port, subprotocol,
    )

    ssh_client: paramiko.SSHClient | None = None
    channel: paramiko.Channel | None = None
    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()

    try:
        w = WORKER_MAP[worker]

        # Abrir canal SSH+direct-tcpip en un thread (operación bloqueante)
        ssh_client, channel = await loop.run_in_executor(
            None,
            _open_vnc_channel_sync,
            w["host"],
            w["port"],
            vnc_port,
        )

        # ── Pump: canal SSH → WebSocket ──────────────────────────────────
        async def ssh_to_ws() -> None:
            try:
                while not stop_event.is_set():
                    # channel.recv es bloqueante → executor para no congelar asyncio
                    data: bytes = await loop.run_in_executor(
                        None, channel.recv, 65536
                    )
                    if not data:
                        # EOF del lado VNC (VM apagada / QEMU terminó)
                        logger.info("VNC EOF recibido de QEMU (worker=%s vm=%s)", worker, vm_name)
                        stop_event.set()
                        break
                    await websocket.send_bytes(data)
            except Exception as exc:
                if not stop_event.is_set():
                    logger.debug("VNC ssh→ws terminó: %s", exc)
                stop_event.set()

        # ── Pump: WebSocket → canal SSH ──────────────────────────────────
        async def ws_to_ssh() -> None:
            try:
                while not stop_event.is_set():
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        logger.info("VNC: navegador cerró WebSocket (worker=%s vm=%s)", worker, vm_name)
                        stop_event.set()
                        break
                    # noVNC envía datos binarios (bytes del protocolo RFB)
                    payload: bytes = (
                        message.get("bytes")
                        or (message.get("text") or "").encode("latin-1")
                    )
                    if payload:
                        await loop.run_in_executor(None, channel.sendall, payload)
            except WebSocketDisconnect:
                stop_event.set()
            except Exception as exc:
                if not stop_event.is_set():
                    logger.debug("VNC ws→ssh terminó: %s", exc)
                stop_event.set()

        # Correr ambos pumps en paralelo; terminar cuando cualquiera pare
        t_ssh = asyncio.create_task(ssh_to_ws())
        t_ws  = asyncio.create_task(ws_to_ssh())
        done, pending = await asyncio.wait(
            [t_ssh, t_ws], return_when=asyncio.FIRST_COMPLETED
        )
        stop_event.set()
        for t in pending:
            t.cancel()
        # Absorber excepciones de las tareas completadas
        for t in done:
            with contextlib.suppress(Exception):
                await t

        logger.info("VNC sesión terminada: worker=%s vm=%s port=%d", worker, vm_name, vnc_port)

    except Exception as exc:
        logger.warning(
            "VNC proxy error: worker=%s vm=%s port=%s — %s",
            worker, vm_name, port_raw, exc,
        )
        with contextlib.suppress(Exception):
            await websocket.send_bytes(b"")  # flush para que el browser detecte cierre
    finally:
        if channel is not None:
            with contextlib.suppress(Exception):
                channel.close()
        if ssh_client is not None:
            with contextlib.suppress(Exception):
                ssh_client.close()
        with contextlib.suppress(Exception):
            await websocket.close()


# ── Proxy WebSocket para noVNC de OpenStack ─────────────────────────────
# noVNC construye el WebSocket como ws://<misma-origin>/websockify?token=XXX
# Este endpoint lo captura y lo reenvía al controller:6080/websockify
# a través del túnel SSH (0.0.0.0:6080 → 192.168.202.1:6080 vía puerto 5821).

OS_WEBSOCKIFY_HOST = os.getenv("OS_WEBSOCKIFY_HOST", "172.17.0.1")
OS_WEBSOCKIFY_PORT = int(os.getenv("OS_WEBSOCKIFY_PORT", "6080"))


@app.websocket("/websockify")
async def ws_websockify_proxy(websocket: WebSocket):
    """
    Proxy WebSocket transparente hacia el websockify de Nova.
    noVNC abre ws://<origin>/websockify?token=XXX → aquí → ws://172.17.0.1:6080/websockify?token=XXX
    """
    import websockets  # type: ignore

    qs = websocket.url.query
    target_url = f"ws://{OS_WEBSOCKIFY_HOST}:{OS_WEBSOCKIFY_PORT}/websockify"
    if qs:
        target_url = f"{target_url}?{qs}"

    # noVNC requiere el subprotocolo "binary" igual que para Linux VNC
    requested = websocket.headers.get("sec-websocket-protocol", "")
    subprotocol = "binary" if "binary" in requested else None

    await websocket.accept(subprotocol=subprotocol)
    logger.info("OpenStack VNC WebSocket: %s", target_url)

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()

    try:
        extra_headers = {}
        if subprotocol:
            extra_headers["Sec-WebSocket-Protocol"] = subprotocol

        async with websockets.connect(
            target_url,
            subprotocols=["binary"] if subprotocol else [],
            extra_headers=extra_headers,
            max_size=10 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        ) as nova_ws:

            async def client_to_nova():
                try:
                    while not stop_event.is_set():
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            stop_event.set()
                            break
                        data = msg.get("bytes") or (msg.get("text") or "").encode("latin-1")
                        if data:
                            await nova_ws.send(data)
                except Exception:
                    stop_event.set()

            async def nova_to_client():
                try:
                    async for msg in nova_ws:
                        if stop_event.is_set():
                            break
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except Exception:
                    stop_event.set()

            t1 = asyncio.create_task(client_to_nova())
            t2 = asyncio.create_task(nova_to_client())
            done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
            stop_event.set()
            for t in pending:
                t.cancel()

    except Exception as exc:
        logger.warning("OpenStack VNC proxy error: %s", exc)
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()


# ── Proxy para consola VNC de OpenStack ─────────────────────────────────
# Las VMs de OpenStack tienen una console_url del tipo:
#   http://controller:6080/vnc_auto.html?path=%3Ftoken%3DXXX
# "controller" es accesible desde app (host) vía túnel SSH en localhost:6080,
# pero NO desde el navegador del usuario. Este proxy reenvía las peticiones
# HTTP y WebSocket de /openstack-vnc/* al controller:6080 a través del host.
#
# El túnel está activo mientras ~/start_tunnels.sh esté corriendo:
#   ssh -NL 0.0.0.0:6080:192.168.202.1:6080 ubuntu@10.20.11.189 -p 5821 &
#
# NOTA: el túnel de puerto 6080 puede que no esté en start_tunnels.sh aún.
# Si falla, agregar esta línea a start_tunnels.sh:
#   ssh -NL 0.0.0.0:6080:192.168.202.1:6080 ubuntu@10.20.11.189 -p 5821 &

OS_NOVNC_URL = os.getenv("OS_NOVNC_URL", "http://172.17.0.1:6080")


@app.api_route(
    "/openstack-vnc/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy_openstack_vnc(path: str, request: Request):
    """
    Proxy transparente hacia el noVNC de Nova (controller:6080).
    El navegador accede a /openstack-vnc/vnc_auto.html?... y este
    endpoint lo reenvía a http://172.17.0.1:6080/vnc_auto.html?...
    """
    target = f"{OS_NOVNC_URL}/{path}"
    qs = request.url.query
    if qs:
        target = f"{target}?{qs}"

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            body = await request.body()
            resp = await client.request(
                method=request.method,
                url=target,
                headers=headers,
                content=body,
            )
        # Filtrar headers que httpx no puede pasar tal cual
        resp_headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() not in ("transfer-encoding", "connection")
        }
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    except httpx.ConnectError:
        raise HTTPException(
            status_code=502,
            detail=(
                "No se puede conectar al noVNC de OpenStack (controller:6080). "
                "Verifica que el túnel SSH esté activo: "
                "ssh -NL 0.0.0.0:6080:192.168.202.1:6080 ubuntu@10.20.11.189 -p 5821 &"
            ),
        )


UI_DIR = Path("/app/ui")

if UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=UI_DIR), name="static")

    @app.get("/")
    async def root():
        return FileResponse(UI_DIR / "index.html")

    @app.get("/{full_path:path}")
    async def serve_pages(full_path: str):
        target = UI_DIR / full_path
        if target.exists() and target.is_file():
            return FileResponse(target)
        return FileResponse(UI_DIR / "index.html")
else:
    @app.get("/")
    async def no_ui():
        return {"message": "UI no encontrada"}
