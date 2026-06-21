/**
 * lib/layout.js
 *
 * Construye el shell de la app: sidebar de navegación (condicional por rol)
 * + contenedor de contenido donde el router pinta cada página.
 */

import { h, roleBadge } from "./components.js";
import { getUser, getRole, canManageUsers, canManageCourses, logout } from "./auth.js";

const NAV_ITEMS = [
  { path: "/", label: "Dashboard", icon: "◧", roles: null },
  { path: "/slices", label: "Slices", icon: "▤", roles: null },
  // Solo admin y profesor pueden crear slices (alumno y coach quedan fuera).
  { path: "/slices/new", label: "Nuevo slice", icon: "+", roles: ["admin", "profesor"] },
  // Monitoreo: admin ve workers físicos; profesor/coach ven slices de sus
  // cursos; alumno NO ve monitoreo (solo sus propios slices desde Dashboard).
  { path: "/monitoring", label: "Monitoreo", icon: "▣", roles: ["admin", "profesor", "coach"] },
];

const ADMIN_NAV_ITEMS = [
  { path: "/admin/users", label: "Usuarios", icon: "☷", roles: ["admin"] },
  { path: "/admin/courses", label: "Cursos", icon: "▥", roles: ["admin", "profesor"] },
];

function itemVisible(item, role) {
  return !item.roles || item.roles.includes(role);
}

export function buildShell() {
  const user = getUser();
  const role = getRole();

  const root = h("div", { id: "app-root" });

  const sidebar = h(
    "div",
    { class: "sidebar" },
    h(
      "div",
      { class: "sidebar-brand" },
      h("div", { class: "brand-name" }, "PUCP Cloud Orchestrator"),
      h("div", { class: "brand-sub" }, "GRUPO 6 · TEL141")
    ),
    h(
      "nav",
      { class: "sidebar-nav" },
      ...NAV_ITEMS.filter((i) => itemVisible(i, role)).map((item) =>
        h(
          "a",
          { class: "nav-link", href: `#${item.path}`, "data-nav-link": "" },
          h("span", { class: "nav-icon" }, item.icon),
          item.label
        )
      ),
      (canManageUsers() || canManageCourses())
        ? h("div", { class: "nav-section-label" }, "Administración")
        : null,
      ...ADMIN_NAV_ITEMS.filter((i) => itemVisible(i, role)).map((item) =>
        h(
          "a",
          { class: "nav-link", href: `#${item.path}`, "data-nav-link": "" },
          h("span", { class: "nav-icon" }, item.icon),
          item.label
        )
      )
    ),
    h(
      "div",
      { class: "sidebar-footer" },
      h(
        "div",
        { class: "sidebar-user" },
        roleBadge(role),
        h("span", { class: "sidebar-user-name" }, user ? user.username : "")
      ),
      h(
        "button",
        { class: "btn btn-ghost sidebar-logout", onClick: () => logout() },
        "Cerrar sesión"
      )
    )
  );

  const main = h("main", { class: "main-content", id: "page-container" });

  root.append(sidebar, main);
  return { root, pageContainer: main };
}
