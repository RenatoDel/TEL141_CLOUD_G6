/**
 * pages/admin-users.js
 *
 * Gestión de usuarios — solo admin. Listar, crear, editar rol/estado,
 * desactivar (soft delete).
 */

import { AuthApi } from "../lib/api.js";
import { h, roleBadge, openModal, showError, showToast, confirmDialog } from "../lib/components.js";
import { isAdmin } from "../lib/auth.js";

export async function renderAdminUsers(container) {
  if (!isAdmin()) {
    container.innerHTML = "";
    container.append(
      h(
        "div",
        { class: "empty-state" },
        h("h2", {}, "Acceso restringido"),
        h("p", {}, "Esta sección es solo para administradores.")
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
        h("h1", {}, "Usuarios"),
        h("div", { class: "page-subtitle" }, "Gestión de cuentas del sistema")
      ),
      h("button", { class: "btn btn-primary", onClick: () => openCreateUserModal(container) }, "+ Nuevo usuario")
    )
  );

  const tableWrap = h("div", { class: "page-loading" }, "Cargando usuarios…");
  container.append(tableWrap);

  let users = [];
  try {
    users = await AuthApi.listUsers();
  } catch (err) {
    tableWrap.replaceWith(h("div", { class: "empty-state empty-state--error" }, "No se pudieron cargar los usuarios."));
    showError(err);
    return;
  }

  const table = buildUsersTable(users, container);
  tableWrap.replaceWith(table);
}

function buildUsersTable(users, container) {
  const table = h(
    "table",
    { class: "data-table" },
    h(
      "thead",
      {},
      h(
        "tr",
        {},
        h("th", {}, "Usuario"),
        h("th", {}, "Email"),
        h("th", {}, "Rol"),
        h("th", {}, "Cursos"),
        h("th", {}, "Estado"),
        h("th", {}, "")
      )
    )
  );
  const tbody = h("tbody", {});
  table.append(tbody);

  for (const u of users) {
    tbody.append(
      h(
        "tr",
        {},
        h("td", { class: "mono" }, u.username),
        h("td", {}, u.email),
        h("td", {}, roleBadge(u.rol)),
        h("td", {}, u.courses && u.courses.length ? u.courses.join(", ") : h("span", { class: "text-faint" }, "—")),
        h("td", {}, u.activo ? h("span", { class: "badge badge--success" }, "Activo") : h("span", { class: "badge badge--neutral" }, "Inactivo")),
        h(
          "td",
          { class: "table-actions" },
          h(
            "button",
            { class: "btn btn-ghost btn-sm", onClick: () => openEditUserModal(u, container) },
            "Editar"
          ),
          u.username !== "admin"
            ? h(
                "button",
                { class: "btn btn-danger btn-sm", onClick: () => handleDeactivate(u, container) },
                "Desactivar"
              )
            : null
        )
      )
    );
  }
  return table;
}

async function openCreateUserModal(container) {
  await openModal({
    title: "Nuevo usuario",
    renderContent: (body, close) => {
      const usernameInput = h("input", { type: "text", id: "new-username", placeholder: "alumno5" });
      const emailInput = h("input", { type: "email", id: "new-email", placeholder: "alumno5@pucp.edu.pe" });
      const passwordInput = h("input", { type: "password", id: "new-password", placeholder: "mínimo 6 caracteres" });
      const roleSelect = h(
        "select",
        { id: "new-role" },
        h("option", { value: "alumno" }, "Alumno"),
        h("option", { value: "profesor" }, "Profesor"),
        h("option", { value: "coach" }, "Coach"),
        h("option", { value: "admin" }, "Admin")
      );

      body.append(
        h("div", { class: "field" }, h("label", {}, "Username"), usernameInput),
        h("div", { class: "field" }, h("label", {}, "Email"), emailInput),
        h("div", { class: "field" }, h("label", {}, "Contraseña"), passwordInput),
        h("div", { class: "field" }, h("label", {}, "Rol"), roleSelect),
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
                  username: usernameInput.value.trim(),
                  email: emailInput.value.trim(),
                  password: passwordInput.value,
                  rol: roleSelect.value,
                };
                if (!payload.username || !payload.email || payload.password.length < 6) {
                  showToast("Completa todos los campos (contraseña ≥ 6 caracteres)", "error");
                  return;
                }
                try {
                  await AuthApi.createUser(payload);
                  showToast(`Usuario ${payload.username} creado`, "success");
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
    if (result) renderAdminUsers(container);
  });
}

async function openEditUserModal(user, container) {
  await openModal({
    title: `Editar ${user.username}`,
    renderContent: (body, close) => {
      const roleSelect = h(
        "select",
        { id: "edit-role" },
        ...["admin", "profesor", "coach", "alumno"].map((r) =>
          h("option", { value: r, selected: r === user.rol ? "selected" : null }, r)
        )
      );
      const activeCheckbox = h("input", { type: "checkbox", id: "edit-active", checked: user.activo ? "checked" : null });
      const passwordInput = h("input", { type: "password", id: "edit-password", placeholder: "deja vacío para no cambiar" });

      body.append(
        h("div", { class: "field" }, h("label", {}, "Rol"), roleSelect),
        h(
          "div",
          { class: "checkbox-field field" },
          activeCheckbox,
          h("label", { for: "edit-active", style: "margin:0" }, "Cuenta activa")
        ),
        h("div", { class: "field" }, h("label", {}, "Nueva contraseña (opcional)"), passwordInput),
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
                  rol: roleSelect.value,
                  activo: activeCheckbox.checked,
                };
                if (passwordInput.value) payload.password = passwordInput.value;
                try {
                  await AuthApi.updateUser(user.username, payload);
                  showToast("Usuario actualizado", "success");
                  close(true);
                } catch (err) {
                  showError(err);
                }
              },
            },
            "Guardar"
          )
        )
      );
    },
  }).then((result) => {
    if (result) renderAdminUsers(container);
  });
}

async function handleDeactivate(user, container) {
  const confirmed = await confirmDialog({
    title: "Desactivar usuario",
    message: `¿Desactivar la cuenta de "${user.username}"? Podrá reactivarse después editando su estado.`,
    confirmLabel: "Desactivar",
    danger: true,
  });
  if (!confirmed) return;

  try {
    await AuthApi.deleteUser(user.username);
    showToast("Usuario desactivado", "success");
    renderAdminUsers(container);
  } catch (err) {
    showError(err);
  }
}
