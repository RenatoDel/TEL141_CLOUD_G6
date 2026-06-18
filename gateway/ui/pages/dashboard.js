/**
 * pages/dashboard.js
 *
 * Vista de inicio: resumen de slices del usuario + estado de los workers
 * (consumo de recursos) tomado de /api/monitoring/summary.
 */

import { SliceApi } from "../lib/api.js";
import { h, statusBadge, showError } from "../lib/components.js";
import { getUser, getRole } from "../lib/auth.js";

export async function renderDashboard(container) {
  const user = getUser();
  const role = getRole();

  container.innerHTML = "";
  container.append(
    h(
      "div",
      { class: "page-header" },
      h(
        "div",
        {},
        h("h1", {}, `Hola, ${user.username}`),
        h(
          "div",
          { class: "page-subtitle" },
          roleLabel(role)
        )
      ),
      h(
        "a",
        { href: "#/slices/new", class: "btn btn-primary" },
        "+ Nuevo slice"
      )
    )
  );

  const statsRow = h("div", { class: "card-grid mb-md" });
  container.append(statsRow);

  const monitoringSection = h(
    "div",
    {},
    h("h2", {}, "Recursos del cluster"),
    h("div", { class: "page-loading" }, "Consultando monitoreo…")
  );
  container.append(monitoringSection);

  // ─── Slices propios / visibles ──────────────────────────────────────
  let slices = [];
  try {
    slices = await SliceApi.listGraphSlices();
  } catch (err) {
    showError(err);
  }

  const activeCount = slices.filter((s) =>
    (s.vms || []).some((vm) => (vm.status || "").toLowerCase() === "active" || (vm.status || "").toLowerCase() === "running")
  ).length;
  const totalVms = slices.reduce((acc, s) => acc + (s.vms || []).length, 0);

  statsRow.append(
    statCard("Slices visibles", slices.length, ""),
    statCard("Slices con VMs activas", activeCount, "", "stat-value--accent"),
    statCard("VMs totales", totalVms, "")
  );

  if (slices.length > 0) {
    const recentList = h(
      "div",
      { class: "card mt-md" },
      h("h3", {}, "Slices recientes"),
      h(
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
            h("th", {}, "Dueño"),
            h("th", {}, "VMs"),
            h("th", {}, "")
          )
        ),
        h(
          "tbody",
          {},
          ...slices.slice(0, 5).map((s) =>
            h(
              "tr",
              {},
              h("td", { class: "mono" }, s.slice_name),
              h("td", {}, s.cluster || "linux"),
              h("td", {}, s.owner_username || "—"),
              h("td", {}, String((s.vms || []).length)),
              h(
                "td",
                { class: "table-actions" },
                h(
                  "a",
                  { href: `#/slices/${encodeURIComponent(s.slice_name)}`, class: "btn btn-ghost btn-sm" },
                  "Ver"
                )
              )
            )
          )
        )
      )
    );
    container.append(recentList);
  }

  // ─── Monitoreo de workers ────────────────────────────────────────────
  try {
    const summary = await SliceApi.monitoringSummary();
    renderMonitoringSummary(monitoringSection, summary);
  } catch (err) {
    monitoringSection.innerHTML = "";
    monitoringSection.append(
      h("h2", {}, "Recursos del cluster"),
      h("p", { class: "text-dim" }, "No se pudo obtener el monitoreo en este momento.")
    );
  }
}

function roleLabel(role) {
  const labels = {
    admin: "Acceso total al sistema",
    profesor: "Gestionas los slices de tus cursos",
    coach: "Acceso de solo lectura para auditoría",
    alumno: "Gestionas tus propios slices",
  };
  return labels[role] || "";
}

function statCard(label, value, meta, valueClass = "") {
  return h(
    "div",
    { class: "stat-card" },
    h("div", { class: "stat-label" }, label),
    h("div", { class: `stat-value ${valueClass}` }, String(value)),
    meta ? h("div", { class: "stat-meta" }, meta) : null
  );
}

function renderMonitoringSummary(section, summary) {
  section.innerHTML = "";
  section.append(h("h2", {}, "Recursos del cluster"));

  const grid = h("div", { class: "card-grid" });
  for (const w of summary.workers || []) {
    grid.append(
      h(
        "div",
        { class: "stat-card" },
        h(
          "div",
          { class: "flex justify-between items-center" },
          h("div", { class: "stat-label" }, w.worker),
          statusBadge(w.status)
        ),
        h(
          "div",
          { class: "mt-md" },
          resourceBar("CPU", w.cpu_percent, 100, "%"),
          resourceBar("RAM", w.mem_used_gb, w.mem_total_gb, "GB"),
          resourceBar("Disco", w.disk_used_gb, w.disk_total_gb, "GB")
        )
      )
    );
  }
  section.append(grid);
}

function resourceBar(label, used, total, unit) {
  const pct = total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0;
  const color = pct > 85 ? "var(--danger)" : pct > 60 ? "var(--warning)" : "var(--accent)";
  return h(
    "div",
    { class: "mt-md", style: "margin-top:8px" },
    h(
      "div",
      { class: "flex justify-between", style: "font-size:0.72rem;color:var(--text-dim)" },
      h("span", {}, label),
      h("span", { class: "mono" }, `${used.toFixed(1)}/${total.toFixed(1)} ${unit}`)
    ),
    h(
      "div",
      { style: "background:var(--border);border-radius:4px;height:5px;margin-top:3px;overflow:hidden" },
      h("div", { style: `background:${color};height:100%;width:${pct}%` })
    )
  );
}
