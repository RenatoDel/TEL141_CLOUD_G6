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
SSH_CONNECT_TIMEOUT = int(os.getenv("WORKER_SSH_TIMEOUT", "12"))

WORKER_MAP = {
    "server1": {
        "host": os.getenv("WORKER_SERVER1_HOST", "10.0.10.1"),
        "port": int(os.getenv("WORKER_SERVER1_PORT", "22")),
    },
    "server2": {
        "host": os.getenv("WORKER_SERVER2_HOST", "10.0.10.2"),
        "port": int(os.getenv("WORKER_SERVER2_PORT", "22")),
    },
    "server3": {
        "host": os.getenv("WORKER_SERVER3_HOST", "10.0.10.3"),
        "port": int(os.getenv("WORKER_SERVER3_PORT", "22")),
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


@app.websocket("/ws/vnc-proxy")
async def ws_vnc_proxy(websocket: WebSocket):
    worker = websocket.query_params.get("worker", "").strip()
    token  = websocket.query_params.get("token", "").strip()
    port_raw = websocket.query_params.get("port", "").strip()

    try:
        _require_worker_and_token(worker, token)
        vnc_port = int(port_raw)
        if vnc_port < 5900 or vnc_port > 65535:
            raise RuntimeError("Puerto VNC inválido")
    except Exception as exc:
        await websocket.close(code=1008, reason=str(exc))
        return

    await websocket.accept()

    ssh_client  = None
    channel     = None
    stop_event  = asyncio.Event()

    try:
        w         = WORKER_MAP[worker]
        ssh_host  = w["host"]   # 10.20.11.189
        ssh_port  = w["port"]   # 5811 / 5812 / 5813

        # Abrir conexión SSH al worker a través del gateway físico
        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        key_path = Path(SSH_KEY_PATH)
        if key_path.exists():
            pkey = paramiko.ECDSAKey.from_private_key_file(str(key_path))
            ssh_client.connect(
                ssh_host, port=ssh_port,
                username=SSH_USER, pkey=pkey,
                timeout=SSH_CONNECT_TIMEOUT,
                look_for_keys=False, allow_agent=False,
            )
        else:
            ssh_client.connect(
                ssh_host, port=ssh_port,
                username=SSH_USER, password=SSH_PASSWORD,
                timeout=SSH_CONNECT_TIMEOUT,
            )

        # Canal direct-tcpip: túnel al puerto VNC local del worker
        transport = ssh_client.get_transport()
        channel = transport.open_channel(
            "direct-tcpip",
            ("127.0.0.1", vnc_port),   # destino en el worker
            ("127.0.0.1", 0),           # origen local (cualquier puerto)
        )
        channel.settimeout(30)

        loop = asyncio.get_event_loop()

        async def ssh_to_ws():
            """Leer datos del canal SSH y enviarlos al WebSocket."""
            while not stop_event.is_set():
                try:
                    data = await loop.run_in_executor(None, lambda: channel.recv(65536))
                    if not data:
                        stop_event.set()
                        break
                    await websocket.send_bytes(data)
                except TimeoutError:
                    continue  # timeout normal, seguir esperando
                except Exception:
                    stop_event.set()
                    break

        async def ws_to_ssh():
            """Leer datos del WebSocket y enviarlos al canal SSH."""
            while not stop_event.is_set():
                try:
                    message = await websocket.receive()
                except WebSocketDisconnect:
                    stop_event.set()
                    break
                if message["type"] == "websocket.disconnect":
                    stop_event.set()
                    break
                payload = message.get("bytes") or (message.get("text") or "").encode()
                if payload:
                    await loop.run_in_executor(None, channel.sendall, payload)

        await asyncio.gather(ssh_to_ws(), ws_to_ssh(), return_exceptions=True)

    except Exception as exc:
        logger.warning("VNC proxy error worker=%s port=%s: %s", worker, port_raw, exc)
    finally:
        if channel:
            with contextlib.suppress(Exception):
                channel.close()
        if ssh_client:
            with contextlib.suppress(Exception):
                ssh_client.close()
        with contextlib.suppress(Exception):
            await websocket.close()


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
