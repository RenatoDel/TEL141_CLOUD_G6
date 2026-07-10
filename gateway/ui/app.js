/**
 * app.js — punto de entrada de la SPA.
 *
 * 1. Verifica sesión activa (si no, redirige a login).
 * 2. Construye el shell (sidebar + contenedor de página).
 * 3. Registra las rutas y arranca el router.
 */

import { requireAuth } from "./lib/auth.js";
import { buildShell } from "./lib/layout.js";
import { registerRoute, startRouter } from "./lib/router.js";

import { renderDashboard } from "./pages/dashboard.js";
import { renderSlicesList } from "./pages/slices-list.js";
import { renderSliceEditor } from "./pages/slice-editor.js";
import { renderSliceDetail } from "./pages/slice-detail.js";
import { renderMonitoring } from "./pages/monitoring.js";
import { renderAdminUsers } from "./pages/admin-users.js";
import { renderAdminCourses } from "./pages/admin-courses.js";

if (!requireAuth()) {
  // requireAuth ya redirigió a /login.html
} else {
  const { root, pageContainer } = buildShell();
  document.getElementById("app-mount").replaceWith(root);

  registerRoute("/", renderDashboard);
  registerRoute("/slices", renderSlicesList);
  registerRoute("/slices/new", renderSliceEditor);
  registerRoute("/slices/:name/edit", renderSliceEditor);
  registerRoute("/slices/:name", renderSliceDetail);
  registerRoute("/monitoring", renderMonitoring);
  registerRoute("/admin/users", renderAdminUsers);
  registerRoute("/admin/courses", renderAdminCourses);

  startRouter(pageContainer);
}
