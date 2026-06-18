/**
 * pages/admin-courses.js
 *
 * Gestión de cursos — admin (crea/borra/reasigna cualquiera) y profesor
 * (edita su propio curso e inscribe/desinscribe alumnos).
 */

import { AuthApi } from "../lib/api.js";
import { h, openModal, showError, showToast, confirmDialog } from "../lib/components.js";
import { isAdmin, canManageCourses } from "../lib/auth.js";

export async function renderAdminCourses(container) {
  if (!canManageCourses()) {
    container.innerHTML = "";
    container.append(
      h(
        "div",
        { class: "empty-state" },
        h("h2", {}, "Acceso restringido"),
        h("p", {}, "Esta sección es para administradores y profesores.")
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
        h("h1", {}, "Cursos"),
        h("div", { class: "page-subtitle" }, isAdmin() ? "Todos los cursos del sistema" : "Cursos que dictas")
      ),
      isAdmin()
        ? h("button", { class: "btn btn-primary", onClick: () => openCreateCourseModal(container) }, "+ Nuevo curso")
        : null
    )
  );

  const wrap = h("div", { class: "page-loading" }, "Cargando cursos…");
  container.append(wrap);

  let courses = [];
  try {
    courses = await AuthApi.listCourses();
  } catch (err) {
    wrap.replaceWith(h("div", { class: "empty-state empty-state--error" }, "No se pudieron cargar los cursos."));
    showError(err);
    return;
  }

  if (courses.length === 0) {
    wrap.replaceWith(h("div", { class: "empty-state" }, h("p", {}, "No hay cursos registrados.")));
    return;
  }

  const grid = h("div", { class: "card-grid" });
  for (const course of courses) {
    grid.append(renderCourseCard(course, container));
  }
  wrap.replaceWith(grid);
}

function renderCourseCard(course, container) {
  return h(
    "div",
    { class: "card" },
    h(
      "div",
      { class: "flex justify-between items-center" },
      h("h3", { style: "margin:0" }, course.codigo),
      h("span", { class: "badge badge--neutral" }, course.periodo)
    ),
    h("p", { class: "text-dim", style: "margin:6px 0" }, course.nombre),
    h(
      "div",
      { style: "font-size:0.78rem;color:var(--text-dim);margin-bottom:10px" },
      `Profesor: ${course.profesor_username || "—"}`
    ),
    h(
      "div",
      { style: "font-size:0.78rem;margin-bottom:12px" },
      h("div", { class: "text-faint", style: "margin-bottom:4px" }, `Alumnos (${course.alumnos.length})`),
      course.alumnos.length
        ? h(
            "div",
            { class: "flex gap-sm", style: "flex-wrap:wrap" },
            ...course.alumnos.map((a) =>
              h(
                "span",
                { class: "badge badge--neutral", style: "cursor:pointer" },
                a,
                " ",
                h(
                  "span",
                  {
                    style: "color:var(--danger);margin-left:4px",
                    onClick: (e) => {
                      e.stopPropagation();
                      handleUnenroll(course.codigo, a, container);
                    },
                  },
                  "×"
                )
              )
            )
          )
        : h("span", { class: "text-faint" }, "Sin alumnos inscritos")
    ),
    h(
      "div",
      { class: "flex gap-sm" },
      h(
        "button",
        { class: "btn btn-ghost btn-sm", onClick: () => openEnrollModal(course, container) },
        "+ Inscribir alumno"
      ),
      isAdmin()
        ? h(
            "button",
            { class: "btn btn-danger btn-sm", onClick: () => handleDeleteCourse(course.codigo, container) },
            "Borrar"
          )
        : null
    )
  );
}

async function openCreateCourseModal(container) {
  let profesores = [];
  try {
    const users = await AuthApi.listUsers();
    profesores = users.filter((u) => u.rol === "profesor");
  } catch {
    profesores = [];
  }

  await openModal({
    title: "Nuevo curso",
    renderContent: (body, close) => {
      const codigoInput = h("input", { type: "text", id: "course-codigo", placeholder: "TEL142" });
      const nombreInput = h("input", { type: "text", id: "course-nombre", placeholder: "Nombre del curso" });
      const profesorSelect = h(
        "select",
        { id: "course-profesor" },
        h("option", { value: "" }, "— Sin asignar —"),
        ...profesores.map((p) => h("option", { value: p.username }, p.username))
      );

      body.append(
        h("div", { class: "field" }, h("label", {}, "Código"), codigoInput),
        h("div", { class: "field" }, h("label", {}, "Nombre"), nombreInput),
        h("div", { class: "field" }, h("label", {}, "Profesor"), profesorSelect),
        h(
          "div",
          { class: "modal-actions" },
          h("button", { class: "btn btn-ghost", onClick: () => close(null) }, "Cancelar"),
          h(
            "button",
            {
              class: "btn btn-primary",
              onClick: async () => {
                const payload = {
                  codigo: codigoInput.value.trim(),
                  nombre: nombreInput.value.trim(),
                };
                if (profesorSelect.value) payload.profesor_username = profesorSelect.value;
                if (!payload.codigo || !payload.nombre) {
                  showToast("Código y nombre son requeridos", "error");
                  return;
                }
                try {
                  await AuthApi.createCourse(payload);
                  showToast(`Curso ${payload.codigo} creado`, "success");
                  close(true);
                } catch (err) {
                  showError(err);
                }
              },
            },
            "Crear"
          )
        )
      );
    },
  }).then((result) => {
    if (result) renderAdminCourses(container);
  });
}

async function openEnrollModal(course, container) {
  let alumnos = [];
  try {
    const users = await AuthApi.listUsers();
    alumnos = users.filter((u) => u.rol === "alumno" && !course.alumnos.includes(u.username));
  } catch {
    alumnos = [];
  }

  if (alumnos.length === 0) {
    showToast("No hay alumnos disponibles para inscribir (o esta vista requiere rol admin para listarlos)", "info");
    return;
  }

  await openModal({
    title: `Inscribir alumno en ${course.codigo}`,
    renderContent: (body, close) => {
      const select = h(
        "select",
        { id: "enroll-select" },
        ...alumnos.map((a) => h("option", { value: a.username }, a.username))
      );
      body.append(
        h("div", { class: "field" }, h("label", {}, "Alumno"), select),
        h(
          "div",
          { class: "modal-actions" },
          h("button", { class: "btn btn-ghost", onClick: () => close(null) }, "Cancelar"),
          h(
            "button",
            {
              class: "btn btn-primary",
              onClick: async () => {
                try {
                  await AuthApi.enrollStudents(course.codigo, [select.value]);
                  showToast(`Alumno ${select.value} inscrito`, "success");
                  close(true);
                } catch (err) {
                  showError(err);
                }
              },
            },
            "Inscribir"
          )
        )
      );
    },
  }).then((result) => {
    if (result) renderAdminCourses(container);
  });
}

async function handleUnenroll(codigo, username, container) {
  const confirmed = await confirmDialog({
    title: "Desinscribir alumno",
    message: `¿Quitar a "${username}" del curso ${codigo}?`,
    confirmLabel: "Desinscribir",
    danger: true,
  });
  if (!confirmed) return;

  try {
    await AuthApi.unenrollStudent(codigo, username);
    showToast("Alumno desinscrito", "success");
    renderAdminCourses(container);
  } catch (err) {
    showError(err);
  }
}

async function handleDeleteCourse(codigo, container) {
  const confirmed = await confirmDialog({
    title: "Borrar curso",
    message: `¿Borrar el curso "${codigo}"? Los alumnos quedarán desinscritos.`,
    confirmLabel: "Borrar",
    danger: true,
  });
  if (!confirmed) return;

  try {
    await AuthApi.deleteCourse(codigo);
    showToast("Curso borrado", "success");
    renderAdminCourses(container);
  } catch (err) {
    showError(err);
  }
}
