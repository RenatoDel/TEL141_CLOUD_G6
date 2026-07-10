from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

import fcntl

STATE_DIR = Path("/app/state")
STATE_DIR.mkdir(parents=True, exist_ok=True)
SLICES_FILE = STATE_DIR / "slices.json"
LOCK_FILE = STATE_DIR / "slices.lock"
_PROCESS_LOCK = RLock()


@contextmanager
def _locked(*, exclusive: bool):
    """Bloqueo combinado entre hilos y procesos/contenedores.

    slice_manager y rq_worker comparten /app/state. El RLock evita carreras
    dentro del mismo proceso y flock evita que dos procesos reescriban
    slices.json al mismo tiempo.
    """
    with _PROCESS_LOCK:
        LOCK_FILE.touch(exist_ok=True)
        with LOCK_FILE.open("r+") as lock_handle:
            fcntl.flock(
                lock_handle.fileno(),
                fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
            )
            try:
                yield
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _read_unlocked() -> list[dict]:
    if not SLICES_FILE.exists():
        return []
    raw = SLICES_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("El estado de slices debe ser una lista JSON")
    return data


def _write_unlocked(data: list[dict]) -> None:
    """Escritura atómica: nunca deja un JSON parcialmente escrito."""
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix="slices.", suffix=".tmp", dir=str(STATE_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, SLICES_FILE)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def list_slices() -> list[dict]:
    with _locked(exclusive=False):
        return _read_unlocked()


def get_slice(slice_name: str) -> dict | None:
    with _locked(exclusive=False):
        data = _read_unlocked()
        return next((x for x in data if x.get("slice_name") == slice_name), None)


def add_slice(item: dict) -> dict:
    with _locked(exclusive=True):
        data = _read_unlocked()
        if any(x.get("slice_name") == item.get("slice_name") for x in data):
            raise ValueError(f"Ya existe un slice con nombre {item.get('slice_name')!r}")
        data.append(item)
        _write_unlocked(data)
    return item


def replace_slice(slice_name: str, item: dict) -> dict | None:
    with _locked(exclusive=True):
        data = _read_unlocked()
        idx = next(
            (i for i, x in enumerate(data) if x.get("slice_name") == slice_name),
            None,
        )
        if idx is None:
            return None
        data[idx] = item
        _write_unlocked(data)
        return item


def delete_slice(slice_name: str) -> dict | None:
    with _locked(exclusive=True):
        data = _read_unlocked()
        found = next(
            (x for x in data if x.get("slice_name") == slice_name), None
        )
        if not found:
            return None
        data = [x for x in data if x.get("slice_name") != slice_name]
        _write_unlocked(data)
        return found


# ════════════════════════════════════════════════════════════════════════════
# Asignación automática de VLAN base
# ════════════════════════════════════════════════════════════════════════════

VLAN_MIN = 100
VLAN_MAX = 3990
VLAN_MARGIN = 10


def _links_count_for_slice(s: dict) -> int:
    links = s.get("links") or []
    if not links:
        links = (s.get("result") or {}).get("links") or []
    return max(len(links), 1)


def next_free_vlan_base(links_needed: int = 1) -> int:
    """Calcula un bloque de VLANs libre sin reservarlo todavía.

    Los borradores con vlan_base=None no consumen VLAN. El valor recién queda
    reservado cuando el registro pasa a queued/active y se persiste.
    """
    with _locked(exclusive=False):
        slices = _read_unlocked()

    max_vlan_used = VLAN_MIN - 1
    for s in slices:
        base = s.get("vlan_base")
        if not base:
            continue
        n_links = _links_count_for_slice(s)
        top = base + n_links - 1
        if top > max_vlan_used:
            max_vlan_used = top

    candidate = max_vlan_used + VLAN_MARGIN + 1
    if candidate % 10 != 0:
        candidate = (candidate // 10 + 1) * 10

    top_needed = candidate + links_needed - 1
    if top_needed > VLAN_MAX:
        raise ValueError(
            f"No hay VLANs disponibles: se necesitan {links_needed} VLAN(s) "
            f"a partir de {candidate} pero el máximo es {VLAN_MAX}. "
            "Borra algunos slices inactivos para liberar espacio."
        )

    return candidate
