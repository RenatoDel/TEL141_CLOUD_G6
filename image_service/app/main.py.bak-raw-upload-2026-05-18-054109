from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="PUCP Image Service", version="0.3.0")

DATA_DIR = Path("/app/data")
FILES_DIR = DATA_DIR / "files"
CATALOG = DATA_DIR / "images.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
FILES_DIR.mkdir(parents=True, exist_ok=True)

LOCK = Lock()


class ImageCreate(BaseModel):
    name: str
    filename: str
    os_type: str = "linux"
    format: str = "qcow2"
    size_gb: float = 0.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    cleaned = cleaned.strip("._")
    if not cleaned:
        raise HTTPException(status_code=400, detail="Nombre de archivo inválido")
    return cleaned


def detect_format(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".qcow2"):
        return "qcow2"
    if lower.endswith(".img"):
        return "img"
    if lower.endswith(".iso"):
        return "iso"
    if lower.endswith(".raw"):
        return "raw"
    return "unknown"


def detect_os_type(filename: str) -> str:
    lower = filename.lower()
    if "cirros" in lower:
        return "cirros"
    if "ubuntu" in lower:
        return "ubuntu"
    if "debian" in lower:
        return "debian"
    if "centos" in lower or "rocky" in lower or "alma" in lower:
        return "linux"
    return "linux"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_catalog() -> list[dict]:
    if not CATALOG.exists():
        return []
    raw = CATALOG.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return json.loads(raw)


def save_catalog(data: list[dict]) -> None:
    CATALOG.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def build_record(*, name: str, filename: str, stored_path: Path, source: str, os_type: str | None = None, fmt: str | None = None) -> dict:
    stat = stored_path.stat()
    size_bytes = stat.st_size
    size_gb = round(size_bytes / (1024 ** 3), 4)

    record = {
        "name": name,
        "filename": filename,
        "stored_filename": stored_path.name,
        "path": str(stored_path),
        "os_type": os_type or detect_os_type(filename),
        "format": fmt or detect_format(filename),
        "size_gb": size_gb,
        "size_bytes": size_bytes,
        "sha256": sha256_file(stored_path),
        "source": source,
        "created_at": now_iso(),
    }
    return record


def ensure_unique_name_or_filename(data: list[dict], name: str, filename: str) -> None:
    if any(img["name"] == name for img in data):
        raise HTTPException(status_code=409, detail="Imagen ya registrada con ese nombre")
    if any(img["filename"] == filename for img in data):
        raise HTTPException(status_code=409, detail="Ya existe una imagen con ese filename")


def get_record_by_name_or_404(name: str) -> dict:
    data = load_catalog()
    target = next((img for img in data if img["name"] == name), None)
    if not target:
        raise HTTPException(status_code=404, detail="Imagen no encontrada")
    return target


@app.on_event("startup")
def init_catalog():
    if not CATALOG.exists():
        default_path = FILES_DIR / "cirros-base.img"
        initial = []
        if default_path.exists():
            initial.append(
                build_record(
                    name="cirros-base.img",
                    filename="cirros-base.img",
                    stored_path=default_path,
                    source="preloaded",
                    os_type="cirros",
                    fmt="qcow2",
                )
            )
        save_catalog(initial)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/images")
def list_images():
    return load_catalog()


@app.get("/images/by-name/{name}")
def get_image_by_name(name: str):
    return get_record_by_name_or_404(name)


@app.get("/images/download-by-name/{name}")
def download_image_by_name(name: str):
    target = get_record_by_name_or_404(name)
    path = Path(target.get("path", ""))
    if not path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado en storage")
    return FileResponse(path, filename=target["stored_filename"], media_type="application/octet-stream")


@app.post("/images")
def create_image(payload: ImageCreate):
    with LOCK:
        data = load_catalog()
        ensure_unique_name_or_filename(data, payload.name, payload.filename)

        fake_path = FILES_DIR / payload.filename
        record = {
            "name": payload.name,
            "filename": payload.filename,
            "stored_filename": payload.filename,
            "path": str(fake_path),
            "os_type": payload.os_type,
            "format": payload.format,
            "size_gb": payload.size_gb,
            "size_bytes": int(payload.size_gb * (1024 ** 3)),
            "sha256": "",
            "source": "manual",
            "created_at": now_iso(),
        }
        data.append(record)
        save_catalog(data)
    return record


@app.post("/images/upload")
async def upload_image(
    name: str = Form(...),
    os_type: str = Form("linux"),
    format: str = Form("qcow2"),
    file: UploadFile = File(...),
):
    original_filename = safe_filename(file.filename or "image.bin")
    stored_filename = original_filename
    stored_path = FILES_DIR / stored_filename

    with LOCK:
        data = load_catalog()
        ensure_unique_name_or_filename(data, name, original_filename)

        with stored_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)

        record = build_record(
            name=name,
            filename=original_filename,
            stored_path=stored_path,
            source="upload",
            os_type=os_type,
            fmt=format,
        )
        data.append(record)
        save_catalog(data)

    return record


@app.post("/images/import-url")
async def import_image_from_url(
    name: str = Form(...),
    url: str = Form(...),
    os_type: str = Form("linux"),
    format: str = Form("qcow2"),
):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="La URL debe usar http o https")

    guessed_filename = safe_filename(Path(parsed.path).name or f"{name}.img")
    stored_path = FILES_DIR / guessed_filename

    with LOCK:
        data = load_catalog()
        ensure_unique_name_or_filename(data, name, guessed_filename)

    try:
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise HTTPException(status_code=400, detail=f"No se pudo descargar la URL: HTTP {resp.status_code}")

                with stored_path.open("wb") as out:
                    async for chunk in resp.aiter_bytes():
                        out.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        if stored_path.exists():
            stored_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Error descargando URL: {exc}") from exc

    with LOCK:
        data = load_catalog()
        record = build_record(
            name=name,
            filename=guessed_filename,
            stored_path=stored_path,
            source="url",
            os_type=os_type,
            fmt=format,
        )
        data.append(record)
        save_catalog(data)

    return record


@app.get("/images/download/{stored_filename}")
def download_image(stored_filename: str):
    safe_name = safe_filename(stored_filename)
    data = load_catalog()

    target = next(
        (
            img for img in data
            if img.get("stored_filename") == safe_name
            or img.get("filename") == safe_name
            or img.get("name") == safe_name
        ),
        None,
    )

    if target:
        path_value = target.get("path")
        resolved_name = (
            target.get("stored_filename")
            or target.get("filename")
            or safe_name
        )
        file_path = Path(path_value) if path_value else (FILES_DIR / resolved_name)
    else:
        resolved_name = safe_name
        file_path = FILES_DIR / safe_name

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"Archivo no encontrado: {file_path}")

    return FileResponse(
        path=str(file_path),
        filename=resolved_name,
        media_type="application/octet-stream",
    )

@app.delete("/images/{name}")
def delete_image(name: str):
    with LOCK:
        data = load_catalog()
        target = next((img for img in data if img["name"] == name), None)
        if not target:
            raise HTTPException(status_code=404, detail="Imagen no encontrada")

        data = [img for img in data if img["name"] != name]
        save_catalog(data)

        path = Path(target.get("path", ""))
        if path.exists() and path.is_file():
            path.unlink(missing_ok=True)

    return {"deleted": True, "name": name}
