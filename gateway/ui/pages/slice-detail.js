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
        h("h1", { class: "mono" }, slice.slice_name),
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
        canWrite()
          ? h(
              "button",
              { class: "btn btn-danger", onClick: () => handleDeleteSlice(slice.slice_name) },
              "Borrar slice"
            )
          : null
      )
    )
  );

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

async function handleDeleteSlice(sliceName) {
  const confirmed = await confirmDialog({
    title: "Borrar slice",
    message: `¿Seguro que quieres borrar "${sliceName}"? Esta acción no se puede deshacer.`,
    confirmLabel: "Borrar",
    danger: true,
  });
  if (!confirmed) return;

  try {
    await SliceApi.deleteGraphSlice(sliceName);
    showToast("Slice borrado", "success");
    navigate("/slices");
  } catch (err) {
    showError(err);
  }
}

function openConsoleInfo(slice, vm) {
  // ── OpenStack: console_url directa de Nova/Horizon ──────────────────
  // La URL apunta a http://controller:6080/vnc_auto.html — no es accesible
  // directamente. El gateway expone un proxy en /openstack-vnc que redirige
  // al controller a través del túnel SSH.
  if (vm.console_url) {
    // Reemplazar "http://controller:6080" por el proxy del gateway
    const proxied = vm.console_url.replace(
      /^https?:\/\/[^/]+/,
      `${window.location.origin}/openstack-vnc`
    );
    window.open(proxied, "_blank", "noopener");
    return;
  }

  // ── Linux: proxy WebSocket del gateway → SSH → QEMU VNC ─────────────
  const worker = vm.server || vm.worker;
  const token  = getToken();
  const vmName = vm.name;

  const params = new URLSearchParams({
    worker,
    port: vm.vnc_port,
    token,
    vm:   vmName,
  });
  const viewerUrl = `${window.location.origin}/vnc-viewer.html?${params.toString()}`;
  window.open(viewerUrl, "_blank", "noopener");
}
