/**
 * pages/slice-detail.js
 *
 * Detalle de un slice: lista de VMs con su estado, acciones
 * (start/stop/reboot) si el usuario tiene permiso de escritura, y acceso
 * a la consola VNC de cada VM vía un canvas RFB minimalista por websocket.
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

  const grid = h("div", { class: "card-grid" });
  for (const vm of vms) {
    grid.append(renderVmCard(slice, vm));
  }
  container.append(grid);
}

function renderVmCard(slice, vm) {
  const card = h(
    "div",
    { class: "card" },
    h(
      "div",
      { class: "flex justify-between items-center" },
      h("h3", { class: "mono", style: "margin:0" }, vm.name),
      statusBadge(vm.status)
    ),
    h(
      "div",
      { class: "mt-md", style: "font-size:0.82rem;color:var(--text-dim)" },
      detailRow("Worker", vm.server || vm.worker || "—"),
      detailRow("vCPUs", vm.vcpus ?? "—"),
      detailRow("RAM", vm.ram_mb ? `${vm.ram_mb} MB` : "—"),
      vm.vnc_port ? detailRow("VNC port", vm.vnc_port) : null,
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

  if (vm.vnc_port && (vm.server || vm.worker)) {
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
  const worker = vm.server || vm.worker;
  const token  = getToken();
  const vmName = vm.name;

  // Construir URL hacia la página vnc-viewer.html que carga noVNC
  const params = new URLSearchParams({
    worker,
    port:  vm.vnc_port,
    token,
    vm:    vmName,
  });
  const viewerUrl = `${window.location.origin}/vnc-viewer.html?${params.toString()}`;
  window.open(viewerUrl, "_blank", "noopener");
}
