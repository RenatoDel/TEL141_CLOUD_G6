# Monitoreo (Prometheus + node_exporter) — Guía de despliegue

Esta guía despliega monitoreo real de recursos para los 6 workers del
proyecto (server1/2/3 en Linux, worker1/2/3 en OpenStack), alimentando
tanto el dashboard de la UI como, más importante, el `placement_service`
(R4) con datos de utilización reales en vez de solo lo declarado al crear
cada VM.

## 0. Resumen de la arquitectura

```
app (host)
 ├─ start_tunnels.sh ── abre túneles SSH 127.0.0.1:9101-9103, 9122-9124
 │                       hacia el puerto 9100 (node_exporter) de cada worker
 ├─ contenedor pucp_prometheus (network_mode: host)
 │    └─ scrapea localhost:9101..9124 cada 15s
 └─ contenedor pucp_slice (red pucp_net)
      └─ PROMETHEUS_URL=http://172.17.0.1:9090 (IP del host visto desde Docker)
```

Por qué esta arquitectura: confirmamos con pruebas reales (ping, nc) que
`app` NO tiene ruta directa a ninguna red interna del laboratorio
(192.168.201.x, 192.168.202.x, ni siquiera 10.0.10.x) — todo pasa por el
gateway vía SSH, igual que ya hacías para las APIs de OpenStack. Por eso
Prometheus necesita los mismos túneles, y por eso corre con
`network_mode: host` (para ver esos túneles, que se abren directo en el
host, no dentro de la red Docker `pucp_net`).

## 1. Instalar node_exporter en los 6 workers

```bash
cd ~/TEL141_CLOUD_G6
chmod +x scripts/install_node_exporter.sh
./scripts/install_node_exporter.sh
```

Esto se conecta a cada worker (vía el gateway, igual que SSH normal) e
instala `node_exporter` v1.8.2 como servicio systemd, escuchando en
`0.0.0.0:9100` dentro de cada worker. Es idempotente: si ya está
instalado y corriendo, lo detecta y no reinstala.

**Nota sobre los workers OpenStack**: en nuestras pruebas pidieron
contraseña SSH (a diferencia de los Linux, que ya tenían
`ssh-copy-id` configurado). El script te la pedirá una vez por cada uno;
no se guarda en ningún lado. Si quieres evitar escribirla cada vez,
puedes configurar `ssh-copy-id` hacia esos workers igual que ya hiciste
para los de Linux (ver tu README sección 2.1), aunque no es obligatorio.

Si algún worker falla con `DOWNLOAD_FAILED`, significa que ese worker en
particular no tiene salida a Internet (a diferencia de server1, que sí
confirmamos que la tiene) — en ese caso avísame y armamos un plan B
(descargar el binario en otro lado y copiarlo por scp).

## 2. Agregar los túneles de monitoreo a tu rutina de reconexión

Reemplaza tu `~/start_tunnels.sh` actual por la versión extendida
incluida en este paquete (`scripts/start_tunnels.sh`), que mantiene
exactamente los túneles de OpenStack que ya tenías y agrega los 6 nuevos
de monitoreo.

```bash
cp scripts/start_tunnels.sh ~/start_tunnels.sh
chmod +x ~/start_tunnels.sh
~/start_tunnels.sh
```

Verifica que los túneles quedaron activos:
```bash
jobs -l
```
Deberías ver 11 procesos `ssh -NL` corriendo (5 de OpenStack + 6 de
monitoreo).

## 3. Copiar la configuración de Prometheus al repo

```bash
cd ~/TEL141_CLOUD_G6
mkdir -p prometheus
cp ruta/al/paquete/prometheus/prometheus.yml prometheus/prometheus.yml
```

## 4. Agregar el servicio Prometheus a docker-compose.yml

Abre `docker-compose.yml` y agrega el bloque del servicio `prometheus`
(contenido completo en `docker-compose.prometheus-patch.yml` de este
paquete) dentro de `services:`, y la línea `prometheus_data:` dentro de
`volumes:` al final del archivo.

```bash
nano ~/TEL141_CLOUD_G6/docker-compose.yml
```

Pega el bloque de servicio (cópialo de `docker-compose.prometheus-patch.yml`)
en cualquier punto dentro de `services:`, y agrega `prometheus_data:` junto
a tus otros volúmenes existentes (`mariadb_data`, `vm_images`,
`slice_state`).

## 5. Apuntar slice_manager al Prometheus real

Edita tu `.env`:
```bash
nano ~/TEL141_CLOUD_G6/.env
```
Cambia:
```
PROMETHEUS_URL=http://10.0.10.4:9090
```
por:
```
PROMETHEUS_URL=http://172.17.0.1:9090
```
(Misma IP que ya usas para `controller` en R5 — es el bridge Docker que
ve al host `app` desde dentro de cualquier contenedor en `pucp_net`.)

## 6. Reemplazar slice_manager/app/main.py

Este paquete incluye una versión corregida de `slice_manager/app/main.py`
con un solo cambio respecto al que ya tenías: la lista de workers
monitoreados ahora es `["server1", "server2", "server3", "worker1",
"worker2", "worker3"]` en vez de incluir el inexistente
`"server4-headnode"`.

```bash
cp ruta/al/paquete/slice_manager/app/main.py ~/TEL141_CLOUD_G6/slice_manager/app/main.py
```

## 7. Levantar todo

```bash
cd ~/TEL141_CLOUD_G6
sudo docker-compose up -d prometheus
sudo docker-compose up -d --no-deps slice_manager
```
(`slice_manager` usa bind-mount para `app/`, así que un `--no-deps`
restart basta; no necesitas `--build` para él. `prometheus` sí se crea
por primera vez con `up -d` normal.)

## 8. Verificar

```bash
# Prometheus ve a los 6 workers como "up"
curl -s http://localhost:9090/api/v1/query?query=up | jq '.data.result[] | {node: .metric.node, value: .value[1]}'

# El endpoint que consume la UI ya trae datos reales
TOK_ADMIN=$(curl -s -X POST http://localhost:8500/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}' | jq -r .access_token)

curl -s http://localhost:8500/api/monitoring/summary -H "Authorization: Bearer $TOK_ADMIN" | jq .
```

Deberías ver los 6 workers (no 4) con `"status": "up"` y valores reales
de CPU/RAM/disco, no ceros. Recarga la página de Monitoreo en la UI —
ahora debería pintar las barras de uso reales.

## 9. Aprovechamiento para R4 (VM Placement)

Esto es lo más valioso de este cambio: el `placement_service` ahora
puede consultar el mismo Prometheus para tomar decisiones de colocación
basadas en utilización real, no solo en lo declarado al crear cada VM —
exactamente lo que pide la rúbrica de EX2 para R4 ("considera no solo la
utilización actual sino el estimado de congestión... cuyo uso aún no se
ve reflejado en las mediciones"). Si quieres, en una próxima sesión
extendemos `placement_service` para que use `http://172.17.0.1:9090`
(la misma URL) y pondere su función objetivo con CPU/RAM real además
del consumo declarado.

## 10. Troubleshooting

| Problema | Causa probable | Fix |
|---|---|---|
| `curl: (7) Failed to connect` a `localhost:9090` desde `app` | El contenedor Prometheus no arrancó | `sudo docker logs pucp_prometheus` |
| Algunos workers en `"status": "down"` | El túnel SSH correspondiente se cayó (se cierran al cerrar la sesión de terminal) | Re-ejecutar `~/start_tunnels.sh` |
| `node_exporter` no responde tras reiniciar el worker | El servicio systemd no quedó habilitado | `ssh -p <puerto> ubuntu@<gateway> "sudo systemctl status node_exporter"` |
| `PROMETHEUS_URL` sigue apuntando a la IP vieja tras editar `.env` | `slice_manager` no recargó el `.env` | `sudo docker-compose up -d --no-deps slice_manager` (recrea el contenedor con las env vars nuevas) |
