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
      const [summary, placementWorkers] = await Promise.all([
        SliceApi.monitoringSummary(),
        SliceApi.placementWorkersStatus().catch(() => []),
      ]);

      // Mergear datos de placement en cada worker del summary
      const placementByName = {};
      for (const pw of placementWorkers) {
        placementByName[pw.name] = pw;
      }
      for (const w of summary.workers || []) {
        const pw = placementByName[w.worker];
        if (pw) {
          w.vcpus_total      = pw.cpu_total;
          w.vcpus_reserved   = pw.vcpus_used;
          w.ram_reserved_mb  = pw.ram_used_gb * 1024;
          w.disk_reserved_gb = pw.disk_used_gb;
          w.disk_capacity_gb = pw.disk_total_gb;
          w.cap_cpu_efectiva = pw.cap_cpu_efectiva;
          w.cap_ram_efectiva = pw.cap_ram_gb_efectiva;
        }
      }

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
 * Dos barras separadas para cualquier recurso — patrón Grafana/Datadog:
 *
 *   CPU    res. 2 vCPUs · total 4 vCPUs
 *   Reservado  [████████░░░░░░░░]  50%
 *   Uso real   [██░░░░░░░░░░░░░░]   4%
 *
 * La barra de reservado usa el color del umbral (cyan/amarillo/rojo).
 * La barra de uso real usa siempre un gris-azulado neutro para no confundir.
 * El badge de overcommit (×N) aparece solo cuando ratio > 1.0.
 */
function dualRow({ label, reservedVal, realVal, pctReserved, pctReal, totalVal, allowOvercommit, overcommitRatio }) {
  const clampedRes  = Math.min(100, Math.max(0, pctReserved || 0));
  const clampedReal = Math.min(100, Math.max(0, pctReal     || 0));

  // Color de la barra reservada según umbral de saturación
  const colorRes  = clampedRes > 85 ? "var(--danger)" : clampedRes > 60 ? "var(--warning)" : "var(--accent)";
  // Uso real: siempre en gris-azulado, claramente distinto del reservado
  const colorReal = "#4b6b8a";

  const showOvercommit = allowOvercommit && overcommitRatio > 1.0;
  const ratioLabel     = showOvercommit ? `×${overcommitRatio.toFixed(1)}` : null;

  return h(
    "div",
    { style: "margin-bottom:14px" },

    // ── Encabezado: label + badge overcommit + valores ───────────────
    h(
      "div",
      { class: "flex justify-between items-center", style: "font-size:0.78rem;color:var(--text-dim);margin-bottom:5px" },
      h(
        "div",
        { style: "display:flex;align-items:center;gap:5px" },
        h("span", { style: "font-weight:500;color:var(--text)" }, label),
        ratioLabel
          ? h("span", {
              style: "background:var(--danger);color:#fff;font-size:0.60rem;padding:1px 5px;border-radius:3px;font-weight:700",
            }, ratioLabel)
          : null
      ),
      h("span", { class: "mono", style: "font-size:0.70rem" },
        `res. ${reservedVal} · real ${realVal} · total ${totalVal}`
      )
    ),

    // ── Barra 1: Reservado ───────────────────────────────────────────
    h(
      "div",
      { style: "display:flex;align-items:center;gap:6px;margin-bottom:3px" },
      h("span", { style: "font-size:0.62rem;color:var(--text-faint);width:52px;text-align:right;flex-shrink:0" }, "reservado"),
      h(
        "div",
        { style: "flex:1;background:var(--border);border-radius:3px;height:5px;overflow:hidden" },
        h("div", { style: `height:100%;width:${clampedRes}%;background:${colorRes};border-radius:3px;transition:width 0.4s` })
      ),
      h("span", { class: "mono", style: `font-size:0.62rem;width:30px;color:${colorRes};text-align:right;flex-shrink:0` },
        `${clampedRes.toFixed(0)}%`
      )
    ),

    // ── Barra 2: Uso real ────────────────────────────────────────────
    h(
      "div",
      { style: "display:flex;align-items:center;gap:6px" },
      h("span", { style: "font-size:0.62rem;color:var(--text-faint);width:52px;text-align:right;flex-shrink:0" }, "uso real"),
      h(
        "div",
        { style: "flex:1;background:var(--border);border-radius:3px;height:5px;overflow:hidden" },
        h("div", { style: `height:100%;width:${clampedReal}%;background:${colorReal};border-radius:3px;transition:width 0.4s` })
      ),
      h("span", { class: "mono", style: `font-size:0.62rem;width:30px;color:${colorReal};text-align:right;flex-shrink:0` },
        `${clampedReal.toFixed(0)}%`
      )
    )
  );
}