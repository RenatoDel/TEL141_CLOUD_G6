from __future__ import annotations
"""
api_gateway/main.py
-------------------
API Gateway :8500
- Sirve el frontend React (dist/)
- Proxy hacia Slice Manager :9002
"""

import httpx
import logging
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

SLICE_MANAGER_URL = "http://localhost:9002"

app = FastAPI(
    title="PUCP Cloud — API Gateway",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------
# Proxy hacia Slice Manager
# ------------------------------------------------------------------

@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request):
    url = f"{SLICE_MANAGER_URL}/{path}"
    
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            body = await request.body()
            response = await client.request(
                method  = request.method,
                url     = url,
                headers = {k: v for k, v in request.headers.items() if k != "host"},
                content = body,
                params  = dict(request.query_params),
            )
        return JSONResponse(
            content    = response.json(),
            status_code= response.status_code,
        )
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Slice Manager no disponible")
    except Exception as e:
        logger.error(f"Error en proxy: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ------------------------------------------------------------------
# Servir frontend React
# ------------------------------------------------------------------

DIST_DIR = Path(__file__).parent / "dist"

if DIST_DIR.exists():
    app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        return FileResponse(DIST_DIR / "index.html")
else:
    @app.get("/")
    async def no_frontend():
        return {"message": "Frontend no desplegado. Corre npm run build y copia dist/ aquí."}