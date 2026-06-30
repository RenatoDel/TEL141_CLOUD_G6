/**
 * pages/slices-list.js
 *
 * Listado de slices visibles para el usuario actual (el backend ya filtra
 * por RBAC). Permite borrar (si tiene permiso) y navegar al detalle.
 *
 * Estado en vivo (módulo de colas): los slices creados después de migrar
 * a Redis+RQ traen un campo "state" (queued/started/active/deleting/failed).
 * Los slices legacy (creados antes de la migración) no tienen ese campo —
 * se tratan como "active" para no romper la tabla.
 */

import { SliceApi } from "../lib/api.js";
import { h, statusBadge, showError, showToast, confirmDialog } from "../lib/components.js";
import { canWrite, getUser } from "../lib/auth.js";

// Estados que indican que el deploy/borrado todavía está en curso en el
// worker RQ (mismo set que usa slice-detail.js).
const PENDING_STATES = new Set(["queued", "started", "deleting", "deferred"]);

export async function renderSlicesList(container) {
  container.innerHTML = "";
  container.append(
    h(
      "div",
      { class: "page-header" },
      h(
        "div",
        {},
        h("h1", {}, "Slices"),
        h("div", { class: "page-subtitle" }, "Topologías de red desplegadas")
      ),
      canWrite()
        ? h("a", { href: "#/slices/new", class: "btn btn-primary" }, "+ Nuevo slice")
        : null
    )
  );

  const listEl = h("div", { class: "page-loading" }, "Cargando slices…");
  container.append(listEl);

  let slices = [];
  try {
    slices = await SliceApi.listGraphSlices();
  } catch (err) {
    listEl.replaceWith(h("div", { class: "empty-state empty-state--error" }, "No se pudieron cargar los slices."));
    showError(err);
    return;
  }

  if (slices.length === 0) {
    listEl.replaceWith(
      h(
        "div",
        { class: "empty-state" },
        h("h2", {}, "Sin slices"),
        h("p", {}, "Aún no hay topologías visibles para tu rol."),
        canWrite()
          ? h("a", { href: "#/slices/new", class: "btn btn-primary" }, "Crear el primero")
          : null
      )
    );
    return;
  }

  const user = getUser();
  const table = h(
    "table",
    { class: "data-table" },
    h(
      "thead",
      {},
      h(
        "tr",
        {},
        h("th", {}, "Nombre"),
        h("th", {}, "Estado"),
        h("th", {}, "Cluster"),
        h("th", {}, "VMs"),
        h("th", {}, "Dueño"),
        h("th", {}, "Curso"),
        h("th", {}, "")
      )
    )
  );
  const tbody = h("tbody", {});
  table.append(tbody);

  for (const slice of slices) {
    const vms = slice.vms || [];
    const isOwner = slice.owner_username === user.username;
    const isPending = PENDING_STATES.has(slice.state);

    tbody.append(
      h(
        "tr",
        {},
        h(
          "td",
          {},
          h("a", { href: `#/slices/${encodeURIComponent(slice.slice_name)}`, class: "mono" }, slice.slice_name)
        ),
        h("td", {}, renderSliceStateBadge(slice.state)),
        h("td", {}, slice.cluster || "linux"),
        h(
          "td",
          {},
          h("span", { class: "mono" }, String(vms.length)),
          " ",
          ...vms.slice(0, 3).map((vm) => statusBadge(vm.status))
        ),
        h(
          "td",
          {},
          slice.owner_username || "—",
          isOwner ? h("span", { class: "text-faint" }, " (tú)") : null
        ),
        h("td", {}, slice.curso_id ? `#${slice.curso_id}` : h("span", { class: "text-faint" }, "—")),
        h(
          "td",
          { class: "table-actions" },
          h(
            "a",
            { href: `#/slices/${encodeURIComponent(slice.slice_name)}`, class: "btn btn-ghost btn-sm" },
            "Ver"
          ),
          canWrite() && !isPending
            ? h(
                "button",
                {
                  class: "btn btn-danger btn-sm",
                  onClick: () => handleDelete(slice.slice_name, container),
                },
                "Borrar"
              )
            : null
        )
      )
    );
  }

  listEl.replaceWith(table);
}

/**
 * Badge de estado para la columna "Estado" de la tabla.
 * Devuelve un badge "Activo" (verde, discreto) si el slice no tiene
 * campo "state" (slices legacy creados antes del módulo de colas) o si
 * vale "active" explícitamente.
 */
function renderSliceStateBadge(state) {
  const labels = {
    queued: ["En cola", "#f0ad4e"],
    started: ["Desplegando…", "#f0ad4e"],
    deleting: ["Borrando…", "#f0ad4e"],
    deferred: ["En cola", "#f0ad4e"],
    failed: ["Error", "#d9534f"],
    active: ["Activo", "#5cb85c"],
  };
  const [text, color] = labels[state] || labels.active;
  return h(
    "span",
    {
      style: `background:${color}22;color:${color};border:1px solid ${color}55;padding:2px 8px;border-radius:4px;font-size:0.72rem;font-weight:600;white-space:nowrap`,
    },
    text
  );
}

async function handleDelete(sliceName, container) {
  const confirmed = await confirmDialog({
    title: "Borrar slice",
    message: `¿Seguro que quieres borrar el slice "${sliceName}"? Esta acción eliminará las VMs, redes y reglas asociadas, y no se puede deshacer.`,
    confirmLabel: "Borrar",
    danger: true,
  });
  if (!confirmed) return;

  try {
    // El backend ahora encola el borrado (202 Accepted, status:"deleting")
    // en lugar de borrarlo de forma síncrona. El worker RQ hace el borrado
    // físico real (VMs, redes, OVS flows) en background.
    await SliceApi.deleteGraphSlice(sliceName);
    showToast(`Borrado de "${sliceName}" encolado, esto puede tardar unos segundos…`, "info");

    // Refrescamos la tabla de inmediato para que el slice aparezca con su
    // badge "Borrando…" (en vez de seguir mostrando el estado anterior
    // como si nada hubiera pasado, y sin el botón "Borrar" duplicado).
    await renderSlicesList(container);

    // Polling en segundo plano: cuando el worker termine, refrescamos la
    // tabla otra vez para que el slice desaparezca solo, sin que el
    // usuario tenga que recargar la página manualmente (F5).
    SliceApi.pollUntilDone(sliceName, { intervalMs: 2500, maxAttempts: 40 })
      .then(() => {
        showToast(`Slice "${sliceName}" borrado`, "success");
        renderSlicesList(container);
      })
      .catch(() => {
        // Si falla o expira el polling, igual refrescamos para reflejar
        // el estado más reciente (puede haber quedado en "failed").
        renderSlicesList(container);
      });
  } catch (err) {
    showError(err);
  }
}