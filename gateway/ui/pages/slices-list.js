/**
 * pages/slices-list.js
 *
 * Listado de slices visibles para el usuario actual (el backend ya filtra
 * por RBAC). Permite borrar (si tiene permiso) y navegar al detalle.
 */

import { SliceApi } from "../lib/api.js";
import { h, statusBadge, showError, showToast, confirmDialog } from "../lib/components.js";
import { canWrite, getUser } from "../lib/auth.js";

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

    tbody.append(
      h(
        "tr",
        {},
        h(
          "td",
          {},
          h("a", { href: `#/slices/${encodeURIComponent(slice.slice_name)}`, class: "mono" }, slice.slice_name)
        ),
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
          canWrite()
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

async function handleDelete(sliceName, container) {
  const confirmed = await confirmDialog({
    title: "Borrar slice",
    message: `¿Seguro que quieres borrar el slice "${sliceName}"? Esta acción eliminará las VMs, redes y reglas asociadas, y no se puede deshacer.`,
    confirmLabel: "Borrar",
    danger: true,
  });
  if (!confirmed) return;

  try {
    await SliceApi.deleteGraphSlice(sliceName);
    showToast(`Slice "${sliceName}" borrado`, "success");
    renderSlicesList(container);
  } catch (err) {
    showError(err);
  }
}
