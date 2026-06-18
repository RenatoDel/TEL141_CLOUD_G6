/**
 * pages/monitoring.js
 *
 * Vista dedicada de monitoreo de recursos del cluster, con auto-refresh
 * cada 10s. Devuelve una función de cleanup que el router invoca al salir
 * de la página (para detener el intervalo).
 */

import { SliceApi } from "../lib/api.js";
import { h, statusBadge, showError } from "../lib/components.js";

const REFRESH_MS = 10000;

export async function renderMonitoring(container) {
  container.innerHTML = "";
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

  const totalsRow = h("div", { class: "card-grid mb-md" });
  const workersGrid = h("div", { class: "card-grid" });
  container.append(totalsRow, workersGrid);

  async function refresh() {
    try {
      const summary = await SliceApi.monitoringSummary();
      renderTotals(totalsRow, summary.totals);
      renderWorkers(workersGrid, summary.workers);
    } catch (err) {
      showError(err);
    }
  }

  await refresh();
  const intervalId = setInterval(refresh, REFRESH_MS);

  return () => clearInterval(intervalId);
}

function renderTotals(container, totals) {
  if (!totals) return;
  container.innerHTML = "";
  container.append(
    statCard("Workers activos", `${totals.workers_up}/${totals.workers_total}`),
    statCard("CPU promedio", `${totals.avg_cpu_percent.toFixed(1)}%`),
    statCard("RAM usada", `${totals.mem_used_gb.toFixed(1)} / ${totals.mem_total_gb.toFixed(1)} GB`),
    statCard("Disco usado", `${totals.disk_used_gb.toFixed(1)} / ${totals.disk_total_gb.toFixed(1)} GB`)
  );
}

function statCard(label, value) {
  return h(
    "div",
    { class: "stat-card" },
    h("div", { class: "stat-label" }, label),
    h("div", { class: "stat-value" }, value)
  );
}

function renderWorkers(container, workers) {
  if (!workers) return;
  container.innerHTML = "";
  for (const w of workers) {
    container.append(
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
          resourceRow("RAM", `${w.mem_used_gb.toFixed(1)} / ${w.mem_total_gb.toFixed(1)} GB`, (w.mem_used_gb / Math.max(w.mem_total_gb, 1)) * 100),
          resourceRow("Disco", `${w.disk_used_gb.toFixed(1)} / ${w.disk_total_gb.toFixed(1)} GB`, (w.disk_used_gb / Math.max(w.disk_total_gb, 1)) * 100)
        )
      )
    );
  }
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
