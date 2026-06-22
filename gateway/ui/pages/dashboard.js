/**
 * pages/dashboard.js
 *
 * Vista de inicio. Cambia según rol:
 *   admin     → métricas globales + workers de AMBOS clusters (Linux/OpenStack)
 *               en dos secciones separadas, cada una con su CPU promedio.
 *   profesor  → resumen de slices de SUS cursos (no expone workers físicos)
 *               + lista de cursos que dicta.
 *   coach     → resumen de slices de los cursos que AUDITA (read-only).
 *   alumno    → SOLO sus slices, agrupados por curso, sin monitoreo y sin
 *               botón "Nuevo slice" (el profesor los crea a su nombre).
 *
 * La separación por cluster (Linux vs OpenStack) cumple el feedback de tener
 * dos secciones distintas con su propio CPU promedio.
 */

import { SliceApi, AuthApi } from "../lib/api.js";
import { h, statusBadge, showError } from "../lib/components.js";
import { getUser, getRole, isAdmin, isAlumno, isProfesor, isCoach, canWrite } from "../lib/auth.js";

// Token de generación: cada llamada a renderDashboard incrementa este contador.
// Las funciones async comprueban si su token sigue siendo el actual antes de
// escribir al DOM — si no lo es, significa que llegó un render más nuevo y
// esta ejecución debe abortarse silenciosamente. Esto previene la duplicación
// de contenido que ocurre cuando el router llama al render dos veces rápido.
let _dashGen = 0;

export async function renderDashboard(container) {
  const gen = ++_dashGen;
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
        h("div", { class: "page-subtitle" }, roleLabel(role))
      ),
      canWrite()
        ? h("a", { href: "#/slices/new", class: "btn btn-primary" }, "+ Nuevo slice")
        : null
    )
  );

  if (isAlumno()) {
    await renderAlumnoDashboard(container, gen);
    return;
  }

  if (isProfesor() || isCoach()) {
    await renderCourseAggregatedDashboard(container, gen);
    return;
  }

  await renderAdminDashboard(container, gen);
}

// ════════════════════════════════════════════════════════════════════════
// Dashboard de ALUMNO: solo sus slices, agrupados por curso
// ════════════════════════════════════════════════════════════════════════
async function renderAlumnoDashboard(container, gen) {
  let slices = [];
  let courses = [];
  try {
    [slices, courses] = await Promise.all([
      SliceApi.listGraphSlices(),
      AuthApi.listCourses(),
    ]);
  } catch (err) {
    showError(err);
  }
  if (gen !== _dashGen) return; // render más nuevo llegó, abortar

  if (courses.length === 0 && slices.length === 0) {
    container.append(
      h(
        "div",
        { class: "empty-state" },
        h("h2", {}, "Sin cursos asignados"),
        h(
          "p",
          {},
          "Tu cuenta de alumno aún no está inscrita en ningún curso. " +
          "Cuando un profesor te inscriba y cree un slice a tu nombre, aparecerá aquí."
        )
      )
    );
    return;
  }

  const sliceByCourse = new Map();
  for (const s of slices) {
    const cid = s.curso_id ?? "sin_curso";
    if (!sliceByCourse.has(cid)) sliceByCourse.set(cid, []);
    sliceByCourse.get(cid).push(s);
  }

  // Cards de cursos
  for (const c of courses) {
    const mySlices = sliceByCourse.get(c.id) || [];
    container.append(renderCourseSection(c, mySlices));
  }

  // Slices "sueltos" (sin curso asignado), por si los hubiera
  const orphans = sliceByCourse.get("sin_curso") || [];
  if (orphans.length > 0) {
    container.append(renderCourseSection({ codigo: "—", nombre: "Slices sin curso", profesor_username: null }, orphans));
  }
}

function renderCourseSection(course, slices) {
  return h(
    "div",
    { class: "card mb-md" },
    h(
      "div",
      { class: "flex justify-between items-center" },
      h(
        "div",
        {},
        h("h3", { style: "margin:0" }, `${course.codigo} — ${course.nombre}`),
        course.profesor_username
          ? h("div", { class: "text-dim", style: "font-size:0.78rem;margin-top:2px" }, `Profesor: ${course.profesor_username}`)
          : null
      ),
      h("span", { class: "badge badge--neutral" }, `${slices.length} slice${slices.length !== 1 ? "s" : ""}`)
    ),
    slices.length === 0
      ? h("p", { class: "text-faint mt-md", style: "font-size:0.85rem" }, "Aún no se te ha asignado ningún slice en este curso.")
      : h(
          "table",
          { class: "data-table mt-md" },
          h(
            "thead",
            {},
            h(
              "tr",
              {},
              h("th", {}, "Slice"),
              h("th", {}, "Cluster"),
              h("th", {}, "VMs"),
              h("th", {}, "")
            )
          ),
          h(
            "tbody",
            {},
            ...slices.map((s) =>
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
                h("td", {}, s.cluster || "linux"),
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
}

// ════════════════════════════════════════════════════════════════════════
// Dashboard de PROFESOR/COACH: agregado por curso (sin workers físicos)
// ════════════════════════════════════════════════════════════════════════
async function renderCourseAggregatedDashboard(container, gen) {
  let data = null;
  let courses = [];
  try {
    [data, courses] = await Promise.all([
      SliceApi.monitoringCoursesSummary(),
      AuthApi.listCourses(),
    ]);
  } catch (err) {
    showError(err);
    return;
  }
  if (gen !== _dashGen) return; // render más nuevo llegó, abortar

  // Stats globales (sumando todos los cursos visibles)
  const allSlices = data.courses.flatMap((c) => c.slices);
  const totalActive = allSlices.reduce((acc, s) => acc + s.vm_active, 0);
  const totalVms = allSlices.reduce((acc, s) => acc + s.vm_count, 0);
  const totalReservedVcpu = data.courses.reduce((acc, c) => acc + c.totals.vcpus_reserved, 0);

  const statsRow = h(
    "div",
    { class: "card-grid mb-md" },
    statCard("Cursos visibles", String(courses.length), ""),
    statCard("Slices totales", String(allSlices.length), ""),
    statCard("VMs activas", `${totalActive} / ${totalVms}`, "", "stat-value--accent"),
    statCard("vCPUs reservados", String(totalReservedVcpu), "Suma de slices visibles")
  );
  container.append(statsRow);

  // Mapa curso_id → curso para mostrar nombre
  const courseById = new Map(courses.map((c) => [c.id, c]));

  // Render por curso
  for (const courseData of data.courses) {
    const c = courseData.curso_id != null ? courseById.get(courseData.curso_id) : null;
    container.append(renderCourseAggregateCard(c, courseData));
  }

  if (data.courses.length === 0) {
    container.append(
      h(
        "div",
        { class: "empty-state mt-md" },
        h("h2", {}, "Sin slices en tus cursos"),
        h(
          "p",
          {},
          isProfesor()
            ? "Cuando crees slices para tus alumnos o para ti mismo, aparecerán agregados aquí."
            : "Cuando los profesores de los cursos que auditas creen slices, aparecerán aquí."
        )
      )
    );
  }
}

function renderCourseAggregateCard(course, courseData) {
  const courseTitle = course
    ? `${course.codigo} — ${course.nombre}`
    : courseData.curso_id != null
      ? `Curso #${courseData.curso_id}`
      : "Slices sin curso asignado";

  const t = courseData.totals;
  return h(
    "div",
    { class: "card mb-md" },
    h(
      "div",
      { class: "flex justify-between items-center" },
      h(
        "div",
        {},
        h("h3", { style: "margin:0" }, courseTitle),
        course?.profesor_username
          ? h("div", { class: "text-dim", style: "font-size:0.78rem;margin-top:2px" }, `Profesor: ${course.profesor_username}`)
          : null
      ),
      h("span", { class: "badge badge--neutral" }, `${t.slices} slice${t.slices !== 1 ? "s" : ""}`)
    ),
    h(
      "div",
      { class: "card-grid mt-md", style: "grid-template-columns:repeat(4,minmax(0,1fr))" },
      miniStat("VMs activas", `${t.vms_active}/${t.vms}`),
      miniStat("vCPUs reservados", String(t.vcpus_reserved)),
      miniStat("RAM reservada", `${(t.ram_mb_reserved / 1024).toFixed(1)} GB`),
      miniStat("Disco reservado", `${t.disk_gb_reserved} GB`)
    ),
    courseData.slices.length > 0
      ? h(
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
            ...courseData.slices.map((s) =>
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
      : null
  );
}

// ════════════════════════════════════════════════════════════════════════
// Dashboard de ADMIN: stats globales + workers por cluster (Linux/OpenStack)
// ════════════════════════════════════════════════════════════════════════
async function renderAdminDashboard(container, gen) {
  const statsRow = h("div", { class: "card-grid mb-md" });
  container.append(statsRow);

  const clusterSection = h("div", {});
  container.append(clusterSection);

  let slices = [];
  try {
    slices = await SliceApi.listGraphSlices();
  } catch (err) {
    showError(err);
  }
  if (gen !== _dashGen) return; // render más nuevo llegó, abortar
  const activeCount = slices.filter((s) =>
    (s.vms || []).some((vm) => {
      const st = (vm.status || "").toLowerCase();
      return st === "active" || st === "running";
    })
  ).length;
  const totalVms = slices.reduce((acc, s) => acc + (s.vms || []).length, 0);

  statsRow.append(
    statCard("Slices visibles", String(slices.length), ""),
    statCard("Slices con VMs activas", String(activeCount), "", "stat-value--accent"),
    statCard("VMs totales", String(totalVms), "")
  );

  if (slices.length > 0) {
    const recentList = h(
      "div",
      { class: "card mt-md mb-md" },
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
    container.insertBefore(recentList, clusterSection);
  }

  // Monitoreo separado por cluster
  try {
    const summary = await SliceApi.monitoringSummary();
    if (gen !== _dashGen) return;
    renderClusterSection(clusterSection, "Linux Cluster", "linux", summary);
    renderClusterSection(clusterSection, "OpenStack Cluster", "openstack", summary);
  } catch (err) {
    clusterSection.append(
      h("p", { class: "text-dim" }, "No se pudo obtener el monitoreo en este momento.")
    );
  }
}

function renderClusterSection(parent, title, clusterKey, summary) {
  const workers = (summary.workers || []).filter((w) => w.cluster === clusterKey);
  const totals = summary.totals_by_cluster?.[clusterKey];

  const sectionHeader = h(
    "div",
    { class: "flex justify-between items-center mt-md" },
    h("h2", {}, title),
    totals
      ? h(
          "span",
          { class: "text-dim", style: "font-size:0.85rem" },
          `CPU promedio: ${totals.avg_cpu_percent.toFixed(1)}% · ${totals.workers_up}/${totals.workers_total} workers`
        )
      : null
  );
  parent.append(sectionHeader);

  if (workers.length === 0) {
    parent.append(
      h("p", { class: "text-faint", style: "font-size:0.85rem" }, "Sin workers reportando en este cluster.")
    );
    return;
  }

  const grid = h("div", { class: "card-grid" });
  for (const w of workers) {
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
  parent.append(grid);
}

// ════════════════════════════════════════════════════════════════════════
// Helpers UI
// ════════════════════════════════════════════════════════════════════════
function roleLabel(role) {
  const labels = {
    admin: "Acceso total al sistema",
    profesor: "Gestionas los slices de tus cursos",
    coach: "Auditas los cursos asignados (solo lectura)",
    alumno: "Visualiza los slices asignados a tu cuenta",
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

function miniStat(label, value) {
  return h(
    "div",
    { class: "stat-card", style: "padding:10px" },
    h("div", { class: "stat-label", style: "font-size:0.7rem" }, label),
    h("div", { class: "stat-value", style: "font-size:1.25rem" }, value)
  );
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
