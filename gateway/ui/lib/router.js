/**
 * lib/router.js
 *
 * Router minimalista basado en hash (#/ruta). Sin dependencias.
 *
 * Uso:
 *   import { registerRoute, startRouter } from "./router.js";
 *   registerRoute("/", renderDashboard);
 *   registerRoute("/slices", renderSlicesList);
 *   registerRoute("/slices/:name", renderSliceDetail);
 *   startRouter(document.getElementById("app"));
 *
 * Cada handler recibe (container, params) y es responsable de pintar
 * su contenido dentro de `container`. Puede ser sync o async.
 */

const routes = [];

function pathToRegex(path) {
  const paramNames = [];
  const pattern = path
    .replace(/\/+$/, "")
    .split("/")
    .map((segment) => {
      if (segment.startsWith(":")) {
        paramNames.push(segment.slice(1));
        return "([^/]+)";
      }
      return segment.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    })
    .join("/");
  return { regex: new RegExp(`^${pattern || "/"}$`), paramNames };
}

export function registerRoute(path, handler) {
  const { regex, paramNames } = pathToRegex(path);
  routes.push({ path, regex, paramNames, handler });
}

function matchRoute(hashPath) {
  for (const route of routes) {
    const match = hashPath.match(route.regex);
    if (match) {
      const params = {};
      route.paramNames.forEach((name, idx) => {
        params[name] = decodeURIComponent(match[idx + 1]);
      });
      return { route, params };
    }
  }
  return null;
}

let containerEl = null;
let currentCleanup = null;

function currentHashPath() {
  const hash = window.location.hash.slice(1) || "/";
  return hash.split("?")[0].replace(/\/+$/, "") || "/";
}

async function render() {
  if (!containerEl) return;

  // Si la página anterior registró una función de limpieza (p.ej. cerrar
  // un websocket de VNC, detener un poll de monitoreo), la ejecutamos.
  if (typeof currentCleanup === "function") {
    try {
      currentCleanup();
    } catch (err) {
      console.error("Error en cleanup de la página anterior", err);
    }
    currentCleanup = null;
  }

  const hashPath = currentHashPath();
  const matched = matchRoute(hashPath);

  if (!matched) {
    containerEl.innerHTML = `
      <div class="empty-state">
        <h2>Página no encontrada</h2>
        <p>La ruta <code>${hashPath}</code> no existe.</p>
        <a href="#/" class="btn btn-primary">Volver al inicio</a>
      </div>`;
    return;
  }

  containerEl.innerHTML = `<div class="page-loading">Cargando…</div>`;
  try {
    const result = await matched.route.handler(containerEl, matched.params);
    if (typeof result === "function") {
      currentCleanup = result;
    }
  } catch (err) {
    console.error("Error renderizando la página", err);
    containerEl.innerHTML = `
      <div class="empty-state empty-state--error">
        <h2>Ocurrió un error</h2>
        <p>${err.message || "Error desconocido"}</p>
      </div>`;
  }

  // Refresca el resaltado del item activo en el sidebar, si existe.
  document.querySelectorAll("[data-nav-link]").forEach((el) => {
    const target = el.getAttribute("href")?.replace("#", "") || "";
    el.classList.toggle("active", target.replace(/\/+$/, "") === hashPath);
  });
}

export function startRouter(container) {
  containerEl = container;
  window.addEventListener("hashchange", render);
  if (!window.location.hash) {
    window.location.hash = "#/";
  }
  render();
}

export function navigate(path) {
  window.location.hash = `#${path}`;
}
