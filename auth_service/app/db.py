"""
Helper de conexión a MariaDB para el auth_service.

Conexiones por request — simples y suficientes para el volumen del proyecto.
Si en el futuro se requiere pool, intercambiar por DBUtils o SQLAlchemy.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor


def _config() -> dict:
    return {
        "host": os.getenv("DB_HOST", "mariadb"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER", "pucp"),
        "password": os.getenv("DB_PASS", "pucp_pass"),
        "database": os.getenv("DB_NAME", "pucp_cloud"),
        "charset": "utf8mb4",
        "autocommit": False,
        "cursorclass": DictCursor,
    }


@contextmanager
def get_conn():
    """
    Context manager: abre conexión, hace commit si todo va bien,
    rollback si hay excepción, y cierra siempre.

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
    """
    conn = pymysql.connect(**_config())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
