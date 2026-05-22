from __future__ import annotations

import json
import os
from collections import defaultdict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="PUCP Placement Service", version="0.1.0")
WORKERS = json.loads(os.getenv("WORKERS_JSON", "[]"))
LOCAL_COUNTS = defaultdict(int)

class PlacementRequest(BaseModel):
    vm_count: int = Field(ge=1)
    availability_zone: str | None = None

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/workers")
def list_workers():
    return [{**w, "assigned_now": LOCAL_COUNTS[w["name"]]} for w in WORKERS]

@app.post("/place")
def place(payload: PlacementRequest):
    workers = WORKERS
    if payload.availability_zone:
        workers = [w for w in WORKERS if w.get("zone") == payload.availability_zone]
    if not workers:
        raise HTTPException(status_code=400, detail="No hay workers disponibles para esa zona")

    ordered = sorted(workers, key=lambda w: (LOCAL_COUNTS[w["name"]], w["name"]))
    assignments: list[str] = []
    for i in range(payload.vm_count):
        worker = ordered[i % len(ordered)]
        assignments.append(worker["name"])
        LOCAL_COUNTS[worker["name"]] += 1
        ordered = sorted(workers, key=lambda w: (LOCAL_COUNTS[w["name"]], w["name"]))
    return {"success": True, "workers": assignments}
