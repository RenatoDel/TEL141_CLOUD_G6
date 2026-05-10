from __future__ import annotations
from ssh_client import get_client_internal

for server in ["server1", "server2", "server3"]:
    with get_client_internal(server) as c:
        out, _ = c.execute("hostname")
        print(f"{server}: {out}")
