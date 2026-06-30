/**
 * pages/slice-detail.js
 *
 * Detalle de un slice: lista de VMs con su estado, acciones
 * (start/stop/reboot) si el usuario tiene permiso de escritura, y acceso
 * a la consola VNC de cada VM vía un canvas RFB minimalista por websocket.
 *
 * También muestra la topología del slice en modo solo-lectura (canvas SVG
 * sin permitir drag, edit, ni delete) y un resumen de qué VMs tienen
 * salida a Internet habilitada.
 *
 * Estado en vivo (módulo de colas): si el slice todavía está "queued",
 * "started" o "deleting" (deploy/borrado corriendo en el worker RQ), se
 * muestra un badge de progreso y se hace polling de
 * GET /graph-slices/{name}/job-status hasta que termine, momento en el
 * cual la página se re-renderiza con los datos finales (VMs, consola, etc).
 *
 * Nota sobre la consola VNC: implementar el protocolo RFB completo está
 * fuera de alcance razonable para este módulo. En su lugar, abrimos el
 * websocket del proxy (gateway /ws/vnc-proxy) y mostramos al usuario el
 * estado de conexión + las credenciales/puerto, con un botón para abrir
 * un visor RFB externo (p.ej. noVNC) si el proyecto ya lo expone en otra
 * ruta. Esto es suficiente para cumplir "proveer tokens/credenciales para
 * acceder a la consola virtual" sin reimplementar RFB en el cliente.
 */

import { SliceApi } from "../lib/api.js";
import { TopologyCanvas } from "../lib/topology-canvas.js";
import { h, statusBadge, showError, showToast, confirmDialog } from "../lib/components.js";
import { canWrite, getToken } from "../lib/auth.js";
import { navigate } from "../lib/router.js";

// Estados que indican que el deploy/borrado todavía está en curso en el
// worker RQ. Mientras el slice esté en uno de estos, se arranca polling.
const PENDING_STATES = new Set(["queued", "started", "deleting", "deferred"]);

export async function renderSliceDetail(container, { name }) {
  container.innerHTML = "";
  container.append(h("div", { class: "page-loading" }, "Cargando slice…"));

  let slices = [];
  try {
    slices = await SliceApi.listGraphSlices();
  } catch (err) {
    container.innerHTML = "";
    container.append(h("div", { class: "empty-state empty-state--error" }, "No se pudo cargar el slice."));
    showError(err);
    return;
  }

  const slice = slices.find((s) => s.slice_name === name);
  if (!slice) {
    container.innerHTML = "";
    container.append(
      h(
        "div",
        { class: "empty-state" },
        h("h2", {}, "Slice no encontrado"),
        h("p", {}, "No existe o no tienes permiso para verlo."),
        h("a", { href: "#/slices", class: "btn btn-primary" }, "Volver al listado")
      )
    );
    return;
  }

  container.innerHTML = "";
  container.append(
    h(
      "div",
      { class: "page-header" },
      h(
        "div",
        {},
        h(
          "div",
          { class: "flex items-center gap-sm" },
          h("h1", { class: "mono", style: "margin:0" }, slice.slice_name),
          renderStateBadge(slice.state)
        ),
        h(
          "div",
          { class: "page-subtitle" },
          `Cluster: ${slice.cluster || "linux"} · Dueño: ${slice.owner_username || "—"}`,
          slice.curso_id ? ` · Curso #${slice.curso_id}` : ""
        )
      ),
      h(
        "div",
        { class: "flex gap-sm" },
        h("a", { href: "#/slices", class: "btn btn-ghost" }, "← Volver"),
        canWrite() && slice.state !== "deleting"
          ? h(
              "button",
              { class: "btn btn-danger", onClick: () => handleDeleteSlice(slice.slice_name, container) },
              "Borrar slice"
            )
          : null
      )
    )
  );

  // ─── Si el deploy/borrado sigue en curso, mostrar progreso y NO pintar
  // las VMs todavía (pueden no existir aún, o estar a medio crear) ──────
  if (PENDING_STATES.has(slice.state)) {
    renderPendingState(container, slice, name);
    return;
  }

  // ─── Si el último job falló, mostrar el error de forma visible ──────
  if (slice.state === "failed") {
    container.append(
      h(
        "div",
        {
          class: "card",
          style:
            "border-color:#d9534f;background:rgba(217,83,79,0.08);margin-bottom:1rem",
        },
        h("h3", { style: "margin:0 0 6px;color:#d9534f" }, "El despliegue falló"),
        h(
          "p",
          { style: "margin:0;font-size:0.85rem;color:var(--text-dim)" },
          slice.error || "Error desconocido. Revisa los logs del worker."
        )
      )
    );
  }

  const vms = slice.vms || [];
  if (vms.length === 0) {
    container.append(h("div", { class: "empty-state" }, h("p", {}, "Este slice no tiene VMs registradas.")));
    return;
  }

  // ─── Lista de VMs (primero) ─────────────────────────────────────────
  container.append(
    h(
      "h2",
      { style: "font-size:1rem;margin:0 0 0.75rem;color:var(--text)" },
      "Máquinas virtuales"
    )
  );
  const grid = h("div", { class: "card-grid" });
  for (const vm of vms) {
    grid.append(renderVmCard(slice, vm));
  }
  container.append(grid);

  // ─── Topología + resumen (debajo de las VMs) ─────────────────────────
  container.append(
    h(
      "h2",
      { style: "font-size:1rem;margin:1.5rem 0 0.75rem;color:var(--text)" },
      "Topología del slice"
    )
  );
  container.append(renderTopologyAndSummary(slice, vms));
}

/**
 * Badge pequeño junto al nombre del slice indicando su estado actual.
 * Devuelve null si el estado es "active" o no está definido (slices
 * legacy creados antes de migrar al módulo de colas no tienen "state").
 */
function renderStateBadge(state) {
  if (!state || state === "active") return null;
  const labels = {
    queued: ["En cola", "#f0ad4e"],
    started: ["Desplegando…", "#f0ad4e"],
    deleting: ["Borrando…", "#f0ad4e"],
    deferred: ["En cola", "#f0ad4e"],
    failed: ["Error", "#d9534f"],
  };
  const [text, color] = labels[state] || [state, "#999"];
  return h(
    "span",
    {
      class: "badge",
      style: `background:${color}22;color:${color};border:1px solid ${color}55;padding:2px 8px;border-radius:4px;font-size:0.72rem;font-weight:600`,
    },
    text
  );
}

/**
 * Vista mostrada mientras el slice está "queued"/"started"/"deleting".
 * Arranca el polling de SliceApi.pollUntilDone y re-renderiza la página
 * completa cuando el job termina (éxito o error).
 */
function renderPendingState(container, slice, sliceName) {
  const isDeleting = slice.state === "deleting";
  const badge = h(
    "span",
    {
      class: "badge",
      style:
        "background:#f0ad4e22;color:#f0ad4e;border:1px solid #f0ad4e55;padding:3px 10px;border-radius:4px;font-size:0.8rem;font-weight:600",
    },
    isDeleting ? "Borrando…" : "Desplegando…"
  );

  const card = h(
    "div",
    { class: "card", style: "text-align:center;padding:2.5rem 1rem" },
    h("div", { style: "margin-bottom:12px" }, badge),
    h(
      "p",
      { style: "color:var(--text-dim);font-size:0.85rem;margin:0" },
      isDeleting
        ? "Liberando recursos y eliminando las VMs del cluster físico. Esto puede tardar hasta un minuto."
        : "Asignando recursos, creando redes y levantando las VMs en el cluster físico. Esto puede tardar entre 30 segundos y 2 minutos."
    )
  );
  container.append(card);

  SliceApi.pollUntilDone(sliceName, {
    onUpdate: (status) => {
      const label =
        status.status === "started"
          ? isDeleting
            ? "Borrando…"
            : "Desplegando…"
          : status.status === "queued" || status.status === "deferred"
          ? "En cola…"
          : status.status;
      badge.textContent = label;
    },
  })
    .then(() => {
      showToast(
        isDeleting ? "Slice borrado correctamente" : "Slice desplegado correctamente",
        "success"
      );
      if (isDeleting) {
        navigate("/slices");
      } else {
        // Re-renderizar la página con los datos finales (VMs, consola, etc.)
        renderSliceDetail(container, { name: sliceName });
      }
    })
    .catch((err) => {
      showError(err);
      // Re-renderizar para mostrar el bloque de error visible (slice.state === "failed")
      renderSliceDetail(container, { name: sliceName });
    });
}

/**
 * Renderiza el bloque superior con dos columnas:
 *   - Izquierda: canvas SVG con la topología (readonly)
 *   - Derecha: resumen (VMs totales, con internet, links, cluster)
 *
 * Si el slice no tiene links (raro pero posible), oculta el canvas y solo
 * muestra el resumen.
 */
function renderTopologyAndSummary(slice, vms) {
  // El grid se apila a 1 columna en pantallas <800px usando minmax/auto-fit.
  // En escritorio queda como 2fr/1fr (canvas a la izquierda, resumen a la derecha).
  const wrap = h("div", {
    class: "card",
    style:
      "display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:16px;align-items:stretch",
  });

  // ─── Columna izquierda: canvas readonly ─────────────────────────────
  const canvasCol = h("div", { style: "min-width:0" });
  canvasCol.append(
    h(
      "h3",
      { style: "margin:0 0 8px;font-size:0.92rem;color:var(--text)" },
      "Topología"
    )
  );

  const links = slice.links || [];
  const hasGraph = links.length > 0;

  if (hasGraph) {
    const svgWrap = h("div", {
      class: "topo-svg-scroll",
      style:
        "background:var(--bg);border-radius:6px;border:1px solid var(--border);max-height:380px;overflow:auto",
    });
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "topo-svg");
    svgWrap.append(svg);
    canvasCol.append(svgWrap);

    // Crear el canvas en modo readonly y cargarle el grafo
    const canvas = new TopologyCanvas(svg, { viewOnly: true });

    // Adaptar las VMs a la forma {name, internet, ...} que espera loadFromGraph
    const nodes = vms.map((vm) => ({
      name: vm.name,
      internet: !!vm.internet,
      vcpus: vm.vcpus,
      ram_mb: vm.ram_mb,
      disk_gb: vm.disk_gb,
      image_name: vm.image_name,
    }));

    // Los links del slice ya vienen como [{id, from, to, vlan_id}], compatible
    canvas.loadFromGraph(nodes, links);
  } else {
    canvasCol.append(
      h(
        "p",
        { class: "text-faint", style: "font-size:0.78rem;margin:8px 0" },
        "Este slice no tiene enlaces declarados."
      )
    );
  }

  // ─── Columna derecha: resumen rápido ────────────────────────────────
  const internetVms = vms.filter((v) => v.internet);
  // "running" = Linux cluster, "active" = OpenStack — ambos significan lo mismo
  const RUNNING_STATES = new Set(["running", "active"]);
  const runningVms = vms.filter((v) => RUNNING_STATES.has((v.status || "").toLowerCase()));

  const summary = h(
    "div",
    {
      style:
        "display:flex;flex-direction:column;gap:8px;font-size:0.82rem;color:var(--text-dim)",
    },
    h(
      "h3",
      { style: "margin:0 0 4px;font-size:0.92rem;color:var(--text)" },
      "Resumen"
    ),
    summaryRow("Cluster", slice.cluster || "linux"),
    summaryRow("VMs totales", String(vms.length)),
    summaryRow("VMs running", `${runningVms.length}/${vms.length}`),
    summaryRow("Enlaces", String(links.length)),
    summaryRow("VLAN base", slice.vlan_base != null ? String(slice.vlan_base) : "—"),
    summaryRow(
      "Modo internet",
      slice.internet_mode === "headnode_nat"
        ? "Headnode NAT"
        : slice.internet_mode === "provider_network"
        ? "Provider network"
        : "Ninguno"
    )
  );

  // Lista de VMs con internet (si hay alguna)
  if (internetVms.length > 0) {
    summary.append(
      h(
        "div",
        {
          style:
            "margin-top:6px;padding:8px 10px;background:rgba(94,234,212,0.08);border-radius:6px;border:1px solid rgba(94,234,212,0.25)",
        },
        h(
          "div",
          {
            style:
              "font-size:0.72rem;color:#5eead4;margin-bottom:4px;text-transform:uppercase;letter-spacing:0.04em",
          },
          "Con acceso a Internet"
        ),
        ...internetVms.map((vm) =>
          h(
            "div",
            { class: "mono", style: "font-size:0.78rem;color:var(--text);line-height:1.5" },
            `· ${vm.name}` +
              (vm.external_ip ? ` → ${vm.external_ip}` : "")
          )
        )
      )
    );
  } else {
    summary.append(
      h(
        "div",
        {
          style:
            "margin-top:6px;padding:8px 10px;background:var(--bg);border-radius:6px;border:1px dashed var(--border);font-size:0.76rem;color:var(--text-faint)",
        },
        "Ninguna VM tiene salida a Internet en este slice."
      )
    );
  }

  wrap.append(canvasCol, summary);
  return wrap;
}

function summaryRow(label, value) {
  return h(
    "div",
    { class: "flex justify-between", style: "padding:2px 0" },
    h("span", {}, label),
    h("span", { class: "mono", style: "color:var(--text)" }, value)
  );
}

function renderVmCard(slice, vm) {
  // Badge de internet (pequeño, al lado del estado)
  const internetBadge = vm.internet
    ? h(
        "span",
        {
          style:
            "background:rgba(94,234,212,0.15);color:#5eead4;font-size:0.66rem;padding:2px 6px;border-radius:4px;text-transform:uppercase;letter-spacing:0.04em;margin-left:6px",
          title: "Esta VM tiene salida a Internet",
        },
        "🌐 internet"
      )
    : null;

  const card = h(
    "div",
    { class: "card" },
    h(
      "div",
      { class: "flex justify-between items-center" },
      h(
        "div",
        { class: "flex items-center" },
        h("h3", { class: "mono", style: "margin:0" }, vm.name),
        internetBadge
      ),
      statusBadge(vm.status)
    ),
    h(
      "div",
      { class: "mt-md", style: "font-size:0.82rem;color:var(--text-dim)" },
      detailRow("Worker", vm.server || vm.worker || "—"),
      detailRow("vCPUs", vm.vcpus ?? "—"),
      detailRow("RAM", vm.ram_mb ? `${vm.ram_mb} MB` : "—"),
      detailRow("Disco", vm.disk_gb ? `${vm.disk_gb} GB` : "—"),
      vm.image_name ? detailRow("Imagen", vm.image_name) : null,
      vm.vnc_port ? detailRow("VNC port", vm.vnc_port) : null,
      vm.console_url ? detailRow("Consola", "noVNC (OpenStack)") : null,
      vm.external_ip ? detailRow("IP externa", vm.external_ip) : null
    )
  );

  if (canWrite()) {
    const actionsRow = h(
      "div",
      { class: "flex gap-sm mt-md" },
      actionButton(slice.slice_name, vm.name, "start", "Iniciar"),
      actionButton(slice.slice_name, vm.name, "stop", "Detener"),
      actionButton(slice.slice_name, vm.name, "reboot", "Reiniciar")
    );
    card.append(actionsRow);
  }

  // Botón de consola: Linux usa vnc_port via proxy SSH,
  // OpenStack usa console_url directa de Horizon/Nova.
  const hasLinuxVnc = vm.vnc_port && (vm.server || vm.worker);
  const hasOsConsole = !!vm.console_url;

  if (hasLinuxVnc || hasOsConsole) {
    card.append(
      h(
        "button",
        {
          class: "btn btn-ghost btn-sm mt-md w-full",
          onClick: () => openConsoleInfo(slice, vm),
        },
        "Ver acceso a consola"
      )
    );
  }

  return card;
}

function detailRow(label, value) {
  return h(
    "div",
    { class: "flex justify-between", style: "padding:3px 0" },
    h("span", {}, label),
    h("span", { class: "mono", style: "color:var(--text)" }, String(value))
  );
}

function actionButton(sliceName, vmName, action, label) {
  return h(
    "button",
    {
      class: "btn btn-ghost btn-sm",
      onClick: async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        try {
          await SliceApi.vmAction(sliceName, vmName, action);
          showToast(`VM ${vmName}: ${label.toLowerCase()} ejecutado`, "success");
          navigate(`/slices/${encodeURIComponent(sliceName)}`);
        } catch (err) {
          showError(err);
          btn.disabled = false;
        }
      },
    },
    label
  );
}

async function handleDeleteSlice(sliceName, container) {
  const confirmed = await confirmDialog({
    title: "Borrar slice",
    message: `¿Seguro que quieres borrar "${sliceName}"? Esta acción no se puede deshacer.`,
    confirmLabel: "Borrar",
    danger: true,
  });
  if (!confirmed) return;

  try {
    await SliceApi.deleteGraphSlice(sliceName);
    showToast("Borrado encolado, esto puede tardar unos segundos…", "info");

    // No usamos navigate() aquí: ya estamos en /slices/{sliceName}, así que
    // navegar a la misma ruta es un no-op para el router (no re-renderiza).
    // En su lugar, mostramos el estado "deleting" directamente en el
    // contenedor actual, igual que si hubiéramos recargado la página.
    container.innerHTML = "";
    renderPendingState(container, { state: "deleting" }, sliceName);
  } catch (err) {
    showError(err);
  }
}

async function openConsoleInfo(slice, vm) {
  // Pedir siempre una URL fresca al backend para evitar tokens expirados.
  // Para OpenStack: Nova genera un token nuevo (válido ~10 min).
  // Para Linux: devuelve el worker y vnc_port para el proxy WebSocket.
  let consoleInfo;
  try {
    consoleInfo = await SliceApi.getVmConsole(slice.slice_name, vm.name);
  } catch (err) {
    showError(err);
    return;
  }

  if (consoleInfo.type === "openstack") {
    // La console_url fresca tiene la forma:
    //   http://controller:6080/vnc_auto.html?path=%3Ftoken%3DXXX
    // Reescribimos el path para que noVNC conecte a /ws-novnc?token=XXX
    const url = new URL(consoleInfo.console_url);
    const rawPath = url.searchParams.get("path") || "";
    const newPath = rawPath.replace(/^\?/, "ws-novnc?");
    url.searchParams.set("path", newPath);
    const proxied = url.toString().replace(
      /^https?:\/\/[^/]+/,
      `${window.location.origin}/openstack-vnc`
    );
    window.open(proxied, "_blank", "noopener");
    return;
  }

  // Linux: proxy WebSocket del gateway → SSH → QEMU VNC
  const worker  = consoleInfo.worker || vm.server || vm.worker;
  const port    = consoleInfo.vnc_port || vm.vnc_port;
  const token   = getToken();
  const params  = new URLSearchParams({ worker, port, token, vm: vm.name });
  const viewerUrl = `${window.location.origin}/vnc-viewer.html?${params.toString()}`;
  window.open(viewerUrl, "_blank", "noopener");
}
