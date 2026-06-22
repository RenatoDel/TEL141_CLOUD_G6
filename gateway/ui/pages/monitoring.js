/**
 * pages/monitoring.js
 *
 * Vista de monitoreo. Comportamiento por rol:
 *   admin             → workers físicos separados por cluster (Linux / OpenStack),
 *                       cada cluster con su CPU promedio.
 *   profesor / coach  → agregado de slices de SUS cursos (no expone workers
 *                       físicos del cluster).
 *   alumno            → no llega aquí (oculto en sidebar y bloqueado abajo).
 *
 * Auto-refresh cada 10s en ambas vistas. Devuelve cleanup para que el
 * router detenga el intervalo al salir.
 */

import { SliceApi } from "../lib/api.js";
import { h, statusBadge, showError } from "../lib/components.js";
import { isAdmin, isAlumno } from "../lib/auth.js";
import { navigate } from "../lib/router.js";

const REFRESH_MS = 10000;

export async function renderMonitoring(container) {
  // Alumno no debería estar aquí (sidebar lo oculta). Si llega por URL,
  // redirige al dashboard.
  if (isAlumno()) {
    navigate("/");
    return;
  }

  container.innerHTML = "";

  if (isAdmin()) {
    return renderAdminMonitoring(container);
  }
  return renderCourseMonitoring(container);
}

// ════════════════════════════════════════════════════════════════════════
// Monitoreo admin: workers físicos por cluster
// ════════════════════════════════════════════════════════════════════════
async function renderAdminMonitoring(container) {
  container.append(
    h(
      "div",
      { class: "page-header" },
      h(
        "div",
        {},
        h("h1", {}, "Monitoreo del cluster"),
        h("div", { class: "page-subtitle" }, "Actualiza automáticamente cada 10 segundos")
      )
    )
  );

  const linuxSection = h("div", { class: "mb-md" });
  const openstackSection = h("div", { class: "mb-md" });
  container.append(linuxSection, openstackSection);

  async function refresh() {
    try {
      const summary = await SliceApi.monitoringSummary();
      renderClusterBlock(linuxSection, "Linux Cluster", "linux", summary);
      renderClusterBlock(openstackSection, "OpenStack Cluster", "openstack", summary);
    } catch (err) {
      showError(err);
    }
  }

  await refresh();
  const intervalId = setInterval(refresh, REFRESH_MS);
  return () => clearInterval(intervalId);
}

function renderClusterBlock(section, title, clusterKey, summary) {
  const workers = (summary.workers || []).filter((w) => w.cluster === clusterKey);
  const totals = summary.totals_by_cluster?.[clusterKey];

  section.innerHTML = "";
  section.append(
    h(
      "div",
      { class: "flex justify-between items-center" },
      h("h2", { style: "margin:0" }, title),
      totals
        ? h(
            "span",
            { class: "text-dim mono", style: "font-size:0.85rem" },
            `${totals.workers_up}/${totals.workers_total} up · CPU prom. ${totals.avg_cpu_percent.toFixed(1)}%`
          )
        : null
    )
  );

  if (totals) {
    section.append(
      h(
        "div",
        { class: "card-grid mb-md mt-md" },
        statCard("Workers", `${totals.workers_up}/${totals.workers_total}`),
        statCard("CPU promedio", `${totals.avg_cpu_percent.toFixed(1)}%`),
        statCard(
          "RAM reservada",
          `${totals.mem_used_gb.toFixed(1)} / ${totals.mem_total_gb.toFixed(1)} GB`
        ),
        statCard(
          "Disco reservado",
          `${totals.disk_used_gb.toFixed(1)} / ${totals.disk_total_gb.toFixed(1)} GB`
        )
      )
    );
  }

  if (workers.length === 0) {
    section.append(h("p", { class: "text-faint" }, "Sin workers reportando."));
    return;
  }

  const grid = h("div", { class: "card-grid" });
  for (const w of workers) {
    grid.append(
      h(
        "div",
        { class: "card" },
        h(
          "div",
          { class: "flex justify-between items-center" },
          h("h3", { style: "margin:0" }, w.worker),
          statusBadge(w.status)
        ),
        h(
          "div",
          { class: "mt-md" },
          resourceRow("CPU", `${w.cpu_percent.toFixed(1)}%`, w.cpu_percent),
          resourceRow(
            "RAM",
            `${w.mem_used_gb.toFixed(1)} / ${w.mem_total_gb.toFixed(1)} GB`,
            (w.mem_used_gb / Math.max(w.mem_total_gb, 1)) * 100
          ),
          resourceRow(
            "Disco",
            `${w.disk_used_gb.toFixed(1)} / ${w.disk_total_gb.toFixed(1)} GB`,
            (w.disk_used_gb / Math.max(w.disk_total_gb, 1)) * 100
          )
        )
      )
    );
  }
  section.append(grid);
}

// ════════════════════════════════════════════════════════════════════════
// Monitoreo de profesor / coach: agregado de slices por curso
// ════════════════════════════════════════════════════════════════════════
async function renderCourseMonitoring(container) {
  container.append(
    h(
      "div",
      { class: "page-header" },
      h(
        "div",
        {},
        h("h1", {}, "Monitoreo de tus cursos"),
        h("div", { class: "page-subtitle" }, "Slices y recursos reservados por curso · refresco cada 10 s")
      )
    )
  );

  const body = h("div", {});
  container.append(body);

  async function refresh() {
    try {
      const data = await SliceApi.monitoringCoursesSummary();
      renderCoursesView(body, data);
    } catch (err) {
      showError(err);
    }
  }

  await refresh();
  const intervalId = setInterval(refresh, REFRESH_MS);
  return () => clearInterval(intervalId);
}

function renderCoursesView(body, data) {
  body.innerHTML = "";

  if (!data.courses || data.courses.length === 0) {
    body.append(
      h(
        "div",
        { class: "empty-state" },
        h("h2", {}, "Sin actividad en tus cursos"),
        h("p", {}, "Cuando haya slices desplegados en tus cursos, aparecerán aquí con sus métricas reservadas.")
      )
    );
    return;
  }

  for (const c of data.courses) {
    const t = c.totals;
    body.append(
      h(
        "div",
        { class: "card mb-md" },
        h(
          "div",
          { class: "flex justify-between items-center" },
          h(
            "h3",
            { style: "margin:0" },
            c.curso_id != null ? `Curso #${c.curso_id}` : "Slices sin curso asignado"
          ),
          h("span", { class: "badge badge--neutral" }, `${t.slices} slice${t.slices !== 1 ? "s" : ""}`)
        ),
        h(
          "div",
          { class: "card-grid mt-md", style: "grid-template-columns:repeat(4,minmax(0,1fr))" },
          statCard("VMs activas", `${t.vms_active}/${t.vms}`),
          statCard("vCPUs reservados", String(t.vcpus_reserved)),
          statCard("RAM reservada", `${(t.ram_mb_reserved / 1024).toFixed(1)} GB`),
          statCard("Disco reservado", `${t.disk_gb_reserved} GB`)
        ),
        h(
          "table",
          { class: "data-table mt-md" },
          h(
            "thead",
            {},
            h(
              "tr",
              {},
              h("th", {}, "Slice"),
              h("th", {}, "Dueño"),
              h("th", {}, "Cluster"),
              h("th", {}, "VMs"),
              h("th", {}, "")
            )
          ),
          h(
            "tbody",
            {},
            ...c.slices.map((s) =>
              h(
                "tr",
                {},
                h(
                  "td",
                  {},
                  h(
                    "a",
                    { href: `#/slices/${encodeURIComponent(s.slice_name)}`, class: "mono" },
                    s.slice_name
                  )
                ),
                h("td", {}, s.owner_username || "—"),
                h("td", {}, s.cluster),
                h("td", {}, `${s.vm_active}/${s.vm_count}`),
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
      )
    );
  }
}

// ════════════════════════════════════════════════════════════════════════
// UI helpers
// ════════════════════════════════════════════════════════════════════════
function statCard(label, value) {
  return h(
    "div",
    { class: "stat-card" },
    h("div", { class: "stat-label" }, label),
    h("div", { class: "stat-value" }, value)
  );
}

function resourceRow(label, text, pct) {
  const clamped = Math.min(100, Math.max(0, pct || 0));
  const color = clamped > 85 ? "var(--danger)" : clamped > 60 ? "var(--warning)" : "var(--accent)";
  return h(
    "div",
    { style: "margin-bottom:10px" },
    h(
      "div",
      { class: "flex justify-between", style: "font-size:0.78rem;color:var(--text-dim)" },
      h("span", {}, label),
      h("span", { class: "mono" }, text)
    ),
    h(
      "div",
      { style: "background:var(--border);border-radius:4px;height:6px;margin-top:4px;overflow:hidden" },
      h("div", { style: `background:${color};height:100%;width:${clamped}%` })
    )
  );
}
