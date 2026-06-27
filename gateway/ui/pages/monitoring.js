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
 * Barras dobles por recurso:
 *   - Barra principal (color): recursos RESERVADOS por el orquestador (MariaDB)
 *   - Capa encima (blanco translúcido): uso REAL del hardware (Prometheus)
 *   - Indicador ×N en rojo: solo aparece cuando hay overcommit activo (ratio > 1.0)
 *   - Disco: sin overcommit, solo muestra reservado vs real
 *
 * Auto-refresh cada 10s en ambas vistas.
 */

import { SliceApi } from "../lib/api.js";
import { h, statusBadge, showError } from "../lib/components.js";
import { isAdmin, isAlumno } from "../lib/auth.js";
import { navigate } from "../lib/router.js";

const REFRESH_MS = 10000;

export async function renderMonitoring(container) {
  if (isAlumno()) {
    navigate("/");
    return;
  }
  container.innerHTML = "";
  if (isAdmin()) return renderAdminMonitoring(container);
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
        h("div", { class: "page-subtitle" }, "Actualiza automáticamente cada 10 segundos · barras: reservado (color) + uso real (blanco)")
      )
    )
  );

  const linuxSection    = h("div", { class: "mb-md" });
  const openstackSection = h("div", { class: "mb-md" });
  container.append(linuxSection, openstackSection);

  async function refresh() {
    try {
      const summary = await SliceApi.monitoringSummary();
      renderClusterBlock(linuxSection,     "Linux Cluster",     "linux",     summary);
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
  const totals  = summary.totals_by_cluster?.[clusterKey];

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
            `CPU promedio: ${totals.avg_cpu_percent.toFixed(1)}% · ${totals.workers_up}/${totals.workers_total} workers`
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
          h("h3", { style: "margin:0" }, w.worker.toUpperCase()),
          statusBadge(w.status)
        ),
        h(
          "div",
          { class: "mt-md" },
          // CPU: reservado (vCPUs comprometidos) vs real (% uso de Prometheus)
          dualRow({
            label:       "CPU",
            reservedVal: `${w.vcpus_reserved ?? 0} vCPUs`,
            realVal:     `${w.cpu_percent.toFixed(1)}%`,
            pctReserved: ((w.vcpus_reserved ?? 0) / Math.max(w.vcpus_total ?? 4, 1)) * 100,
            pctReal:     w.cpu_percent,
            totalVal:    `${w.vcpus_total ?? "?"} vCPUs`,
            allowOvercommit: true,
            overcommitRatio: w.vcpus_total
              ? (w.vcpus_reserved ?? 0) / w.vcpus_total
              : 0,
          }),
          // RAM: reservado (MB de MariaDB) vs real (uso de Prometheus)
          dualRow({
            label:       "RAM",
            reservedVal: `${((w.ram_reserved_mb ?? 0) / 1024).toFixed(1)} GB`,
            realVal:     `${w.mem_used_gb.toFixed(1)} GB`,
            pctReserved: ((w.ram_reserved_mb ?? 0) / 1024 / Math.max(w.mem_total_gb, 0.01)) * 100,
            pctReal:     (w.mem_used_gb / Math.max(w.mem_total_gb, 0.01)) * 100,
            totalVal:    `${w.mem_total_gb.toFixed(1)} GB`,
            allowOvercommit: true,
            overcommitRatio: w.mem_total_gb
              ? (w.ram_reserved_mb ?? 0) / 1024 / w.mem_total_gb
              : 0,
          }),
          // Disco: reservado (GB de MariaDB) vs real (uso físico de Prometheus)
          // Sin overcommit: pctReserved nunca debería superar 100%
          dualRow({
            label:       "Disco",
            reservedVal: `${(w.disk_reserved_gb ?? w.disk_used_gb).toFixed(1)} GB`,
            realVal:     `${w.disk_used_gb.toFixed(1)} GB`,
            pctReserved: ((w.disk_reserved_gb ?? w.disk_used_gb) / Math.max(w.disk_capacity_gb ?? w.disk_total_gb, 0.01)) * 100,
            pctReal:     (w.disk_used_gb / Math.max(w.disk_total_gb, 0.01)) * 100,
            totalVal:    `${(w.disk_capacity_gb ?? w.disk_total_gb).toFixed(1)} GB`,
            allowOvercommit: false,
            overcommitRatio: 0,
          })
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
            h("tr", {},
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
              h("tr", {},
                h("td", {}, h("a", { href: `#/slices/${encodeURIComponent(s.slice_name)}`, class: "mono" }, s.slice_name)),
                h("td", {}, s.owner_username || "—"),
                h("td", {}, s.cluster),
                h("td", {}, `${s.vm_active}/${s.vm_count}`),
                h("td", { class: "table-actions" },
                  h("a", { href: `#/slices/${encodeURIComponent(s.slice_name)}`, class: "btn btn-ghost btn-sm" }, "Ver")
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

/**
 * Barra doble para cualquier recurso:
 *   - Barra principal (color): % reservado por el orquestador
 *   - Capa encima (blanco translúcido): % uso real del hardware
 *   - Indicador ×N rojo: solo cuando overcommit activo (ratio > 1.0)
 *
 * Para disco: allowOvercommit=false, sin indicador de ratio.
 */
function dualRow({ label, reservedVal, realVal, pctReserved, pctReal, totalVal, allowOvercommit, overcommitRatio }) {
  // Clamp a 100% visualmente (el overcommit se indica con el badge, no desbordando la barra)
  const clampedRes  = Math.min(100, Math.max(0, pctReserved || 0));
  const clampedReal = Math.min(100, Math.max(0, pctReal     || 0));

  const colorRes  = clampedRes > 85 ? "var(--danger)" : clampedRes > 60 ? "var(--warning)" : "var(--accent)";
  const colorReal = "rgba(255,255,255,0.22)";

  // Indicador de overcommit: solo si ratio > 1.0 y el recurso lo permite
  const showOvercommit = allowOvercommit && overcommitRatio > 1.0;
  const ratioLabel     = showOvercommit ? `×${overcommitRatio.toFixed(1)}` : null;

  return h(
    "div",
    { style: "margin-bottom:12px" },
    // ── Fila de encabezado ───────────────────────────────────────────
    h(
      "div",
      { class: "flex justify-between items-center", style: "font-size:0.78rem;color:var(--text-dim)" },
      h(
        "div",
        { style: "display:flex;align-items:center;gap:5px" },
        h("span", {}, label),
        // Badge de overcommit (×1.5 etc.) en rojo, solo cuando activo
        ratioLabel
          ? h("span", {
              style: "background:var(--danger);color:#fff;font-size:0.62rem;padding:1px 5px;border-radius:3px;font-weight:600",
            }, ratioLabel)
          : null
      ),
      h(
        "span",
        { class: "mono", style: "font-size:0.72rem" },
        `res. ${reservedVal} · real ${realVal} · total ${totalVal}`
      )
    ),
    // ── Barra doble ──────────────────────────────────────────────────
    h(
      "div",
      { style: "position:relative;background:var(--border);border-radius:4px;height:6px;margin-top:4px;overflow:hidden" },
      // Capa 1 — reservado (color según umbral, clampeado a 100%)
      h("div", { style: `position:absolute;left:0;top:0;height:100%;width:${clampedRes}%;background:${colorRes};border-radius:4px` }),
      // Capa 2 — uso real (blanco translúcido encima)
      h("div", { style: `position:absolute;left:0;top:0;height:100%;width:${clampedReal}%;background:${colorReal};border-radius:4px` })
    ),
    // ── Leyenda ──────────────────────────────────────────────────────
    h(
      "div",
      { style: "display:flex;gap:10px;margin-top:3px;font-size:0.64rem;color:var(--text-faint)" },
      h("span", {},
        h("span", { style: `display:inline-block;width:8px;height:8px;border-radius:2px;background:${colorRes};margin-right:3px;vertical-align:middle` }),
        "reservado"
      ),
      h("span", {},
        h("span", { style: "display:inline-block;width:8px;height:8px;border-radius:2px;background:rgba(255,255,255,0.35);margin-right:3px;vertical-align:middle" }),
        "uso real"
      )
    )
  );
}