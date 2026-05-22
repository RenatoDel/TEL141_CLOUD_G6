from __future__ import annotations

import json
from pathlib import Path
from threading import Lock

STATE_DIR = Path("/app/state")
STATE_DIR.mkdir(parents=True, exist_ok=True)
SLICES_FILE = STATE_DIR / "slices.json"
_LOCK = Lock()


def _read() -> list[dict]:
    if not SLICES_FILE.exists():
        return []
    raw = SLICES_FILE.read_text().strip()
    if not raw:
        return []
    return json.loads(raw)


def _write(data: list[dict]) -> None:
    SLICES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def list_slices() -> list[dict]:
    with _LOCK:
        return _read()


def get_slice(slice_name: str) -> dict | None:
    with _LOCK:
        data = _read()
        return next((x for x in data if x["slice_name"] == slice_name), None)


def add_slice(item: dict) -> dict:
    with _LOCK:
        data = _read()
        data.append(item)
        _write(data)
    return item


def replace_slice(slice_name: str, item: dict) -> dict | None:
    with _LOCK:
        data = _read()
        idx = next((i for i, x in enumerate(data) if x["slice_name"] == slice_name), None)
        if idx is None:
            return None
        data[idx] = item
        _write(data)
        return item


def delete_slice(slice_name: str) -> dict | None:
    with _LOCK:
        data = _read()
        found = next((x for x in data if x["slice_name"] == slice_name), None)
        if not found:
            return None
        data = [x for x in data if x["slice_name"] != slice_name]
        _write(data)
        return found
