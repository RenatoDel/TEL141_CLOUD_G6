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


# ════════════════════════════════════════════════════════════════════════════
# Asignación automática de VLAN base
# ════════════════════════════════════════════════════════════════════════════

VLAN_MIN = 100      # primera VLAN usable
VLAN_MAX = 3990     # límite práctico (802.1Q va hasta 4094, dejamos margen)
VLAN_MARGIN = 10    # VLANs de separación entre slices consecutivos


def _links_count_for_slice(s: dict) -> int:
    """
    Cuántas VLANs consume este slice guardado.
    Cada link ocupa exactamente 1 VLAN (vlan_base + offset por link).
    Soporta el campo 'links' directo o anidado en result.links.
    """
    links = s.get("links") or []
    if not links:
        links = (s.get("result") or {}).get("links") or []
    return max(len(links), 1)  # mínimo 1 por si los links no están guardados aún


def next_free_vlan_base(links_needed: int = 1) -> int:
    """
    Calcula el próximo vlan_base libre para un slice nuevo que necesita
    `links_needed` VLANs consecutivas.

    Garantías:
    - No solapa con ningún slice existente.
    - Deja VLAN_MARGIN VLANs de separación entre slices.
    - El resultado es siempre múltiplo de 10 (legibilidad en el switch).
    - Thread-safe: usa el mismo _LOCK que el resto del módulo.

    Raises:
        ValueError: si no hay VLANs disponibles en el rango VLAN_MIN–VLAN_MAX.
    """
    with _LOCK:
        slices = _read()

    max_vlan_used = VLAN_MIN - 1

    for s in slices:
        base = s.get("vlan_base")
        if not base:
            continue
        n_links = _links_count_for_slice(s)
        # El slice ocupa VLANs [base, base + n_links - 1]
        top = base + n_links - 1
        if top > max_vlan_used:
            max_vlan_used = top

    # Candidato: tras el último VLAN usado + margen de seguridad
    candidate = max_vlan_used + VLAN_MARGIN + 1

    # Redondear al próximo múltiplo de 10
    if candidate % 10 != 0:
        candidate = (candidate // 10 + 1) * 10

    # Validar que cabe el slice nuevo dentro del rango permitido
    top_needed = candidate + links_needed - 1
    if top_needed > VLAN_MAX:
        raise ValueError(
            f"No hay VLANs disponibles: se necesitan {links_needed} VLAN(s) "
            f"a partir de {candidate} pero el máximo es {VLAN_MAX}. "
            "Borra algunos slices inactivos para liberar espacio."
        )

    return candidate
