#!/bin/bash
# ============================================================================
# start_tunnels.sh (EXTENDIDO)
#
# Combina los túneles originales hacia OpenStack (Keystone/Nova/Neutron/
# Glance/Placement) con los nuevos túneles de monitoreo (node_exporter,
# puerto 9100) hacia los 6 workers de ambos clusters.
#
# Ejecutar tras CADA reconexión a `app` (los túneles SSH no sobreviven
# el cierre de la sesión de terminal que los lanzó).
# ============================================================================

GATEWAY_HOST="10.20.11.189"

echo "--- Túneles OpenStack (Keystone/Nova/Neutron/Glance/Placement) ---"
ssh -NL 0.0.0.0:5000:192.168.202.1:5000 ubuntu@${GATEWAY_HOST} -p 5821 &
ssh -NL 0.0.0.0:8774:192.168.202.1:8774 ubuntu@${GATEWAY_HOST} -p 5821 &
ssh -NL 0.0.0.0:9696:192.168.202.1:9696 ubuntu@${GATEWAY_HOST} -p 5821 &
ssh -NL 0.0.0.0:9292:192.168.202.1:9292 ubuntu@${GATEWAY_HOST} -p 5821 &
ssh -NL 0.0.0.0:8778:192.168.202.1:8778 ubuntu@${GATEWAY_HOST} -p 5821 &

echo "--- Túneles de monitoreo: cluster Linux (server1/2/3 → node_exporter) ---"
# server1: gateway puerto 5811, IP interna 192.168.201.1
ssh -NL 127.0.0.1:9101:192.168.201.1:9100 ubuntu@${GATEWAY_HOST} -p 5811 &
# server2: gateway puerto 5812, IP interna 192.168.201.2
ssh -NL 127.0.0.1:9102:192.168.201.2:9100 ubuntu@${GATEWAY_HOST} -p 5812 &
# server3: gateway puerto 5813, IP interna 192.168.201.3
ssh -NL 127.0.0.1:9103:192.168.201.3:9100 ubuntu@${GATEWAY_HOST} -p 5813 &

echo "--- Túneles de monitoreo: cluster OpenStack (worker1/2/3 → node_exporter) ---"
# worker1: gateway puerto 5822, IP interna 192.168.202.2
ssh -NL 127.0.0.1:9122:192.168.202.2:9100 ubuntu@${GATEWAY_HOST} -p 5822 &
# worker2: gateway puerto 5823, IP interna 192.168.202.3
ssh -NL 127.0.0.1:9123:192.168.202.3:9100 ubuntu@${GATEWAY_HOST} -p 5823 &
# worker3: gateway puerto 5824, IP interna 192.168.202.4
ssh -NL 127.0.0.1:9124:192.168.202.4:9100 ubuntu@${GATEWAY_HOST} -p 5824 &

sleep 2

echo "--- Configurando /etc/hosts dentro de pucp_slice (controller → host) ---"
sudo docker exec pucp_slice bash -c "echo '172.17.0.1 controller' >> /etc/hosts" 2>/dev/null || \
  echo "  (pucp_slice no disponible o ya configurado, omitiendo)"

echo
echo "Tunnels activos: OpenStack API + monitoreo (Linux + OpenStack)"
echo "Verifica con: jobs -l"
