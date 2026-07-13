# PUCP Private Cloud Orchestrator — Grupo 6

Stack Docker que despliega un orquestador de slices de VMs sobre clusters Linux (KVM) y OpenStack. Toda la lógica corre en la **VM app** (`10.20.11.113`).

---

## Requisitos previos

- Docker + Docker Compose instalados en la VM app
- Acceso SSH al gateway (`10.20.11.189`) desde la VM app sin contraseña
- OpenStack operativo con Keystone, Nova, Neutron, Glance y Placement
- Archivo `.env` configurado (ver sección siguiente)

---

## Configuración del .env

Copia `.env.example` a `.env` y ajusta:

```env
# OpenStack
OS_AUTH_URL=http://controller:5000/v3
OS_USERNAME=cloud_admin
OS_PASSWORD=<password>
OS_PROJECT_NAME=cloud_admin
OS_USER_DOMAIN_NAME=Cloud
OS_PROJECT_DOMAIN_NAME=Cloud

# Placement
PROMETHEUS_URL=http://172.17.0.1:9090
REDIS_URL=redis://redis:6379/0
RECONCILE_INTERVAL=300
RISK_FACTOR_K=1.0

# Networking
OS_PHYSNET=physnet1
OS_EXTERNAL_NETWORK_NAME=external
```

---

## Primer arranque

```bash
# 1. Levantar todos los contenedores
sudo docker-compose up -d

# 2. Verificar que los 12 contenedores están Up
sudo docker ps --format "table {{.Names}}\t{{.Status}}"

# 3. Levantar los túneles SSH (OpenStack APIs + monitoreo)
~/start_tunnels.sh
```

La UI queda disponible en `http://10.20.11.113:8500`

---

## Después de cada git pull

| Qué cambió | Comando |
|---|---|
| `slice_manager/app/` o `gateway/ui/` | `sudo docker-compose restart slice_manager rq_worker` — solo recargar navegador para la UI |
| `placement_service/` o `reconcile.py` | `sudo docker-compose up -d --build pucp_placement pucp_reconcile` |
| `gateway/app/` | `sudo docker-compose up -d --build pucp_gateway` |
| `docker-compose.yml` | `sudo docker-compose up -d` |

---

## Cada vez que reconectas a la VM app

```bash
cd ~/TEL141_CLOUD_G6
~/start_tunnels.sh

# Regenerar token admin para usar con curl
TOK=$(curl -s -X POST http://localhost:8500/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}' | jq -r .access_token)
```

---

## Usuarios disponibles

| Usuario | Password | Rol |
|---|---|---|
| admin | admin123 | Administrador total |
| profesor1 | profesor123 | Crea slices para sus alumnos |
| coach1 | coach123 | Supervisa cursos asignados |
| alumno1 | alumno123 | Solo lectura + consola |

---

## Acceso a los workers

Todos vía gateway `ubuntu@10.20.11.189`:

```bash
# Linux cluster
ssh ubuntu@10.20.11.189 -p 5811   # server1
ssh ubuntu@10.20.11.189 -p 5812   # server2
ssh ubuntu@10.20.11.189 -p 5813   # server3
ssh ubuntu@10.20.11.189 -p 5814   # server4 / headnode

# OpenStack cluster
ssh ubuntu@10.20.11.189 -p 5821   # headnode
ssh ubuntu@10.20.11.189 -p 5822   # worker1
ssh ubuntu@10.20.11.189 -p 5823   # worker2
ssh ubuntu@10.20.11.189 -p 5824   # worker3
```

---

## Diagnóstico rápido

```bash
# Ver logs del worker de jobs
sudo docker-compose logs rq_worker --tail 30

# Ver capacidades efectivas del placement
curl -s http://localhost:8500/api/placement/workers/status \
  -H "Authorization: Bearer $TOK" | python3 -m json.tool

# Ver slices activos
curl -s http://localhost:8500/api/graph-slices \
  -H "Authorization: Bearer $TOK" | python3 -m json.tool

# Verificar QEMU en workers Linux
for p in 5811 5812 5813; do
  echo "server$((p-5810)):"
  ssh ubuntu@10.20.11.189 -p $p \
    "ps aux | grep [q]emu-system | grep -v grep || echo vacio"
done
```

---

## Imágenes disponibles

Las imágenes se gestionan desde la UI en **Imágenes** o por API:

```bash
# Listar imágenes registradas
curl -s http://localhost:8500/api/images -H "Authorization: Bearer $TOK"

# Importar imagen por URL (el servidor la descarga solo)
curl -s -X POST http://localhost:8500/api/images/import-url \
  -H "Authorization: Bearer $TOK" \
  -F "name=ubuntu-22.04" \
  -F "url=https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img" \
  -F "os_type=ubuntu" \
  -F "format=qcow2"
```

Para OpenStack, la imagen también debe estar en Glance:
```bash
ssh ubuntu@10.20.11.189 -p 5821 \
  ". ~/env-scripts/cloud-admin-openrc && openstack image list"
```

---

## Credenciales de VMs

| Imagen | Usuario | Password |
|---|---|---|
| ubuntu-22.04 | ubuntu | ubuntu |
| cirros | cirros | gocubsgo |
