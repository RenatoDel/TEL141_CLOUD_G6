/**
 * lib/components.js
 *
 * Helpers de UI reutilizables sin dependencias externas: toasts,
 * modales de confirmación, y un pequeño helper de creación de
 * elementos para no repetir document.createElement en todas partes.
 */

// ════════════════════════════════════════════════════════════════════════
// Toasts (notificaciones efímeras)
// ════════════════════════════════════════════════════════════════════════
let toastContainer = null;

function ensureToastContainer() {
  if (!toastContainer) {
    toastContainer = document.createElement("div");
    toastContainer.className = "toast-container";
    document.body.appendChild(toastContainer);
  }
  return toastContainer;
}

/**
 * @param {string} message
 * @param {"info"|"success"|"error"} kind
 */
export function showToast(message, kind = "info") {
  const container = ensureToastContainer();
  const toast = document.createElement("div");
  toast.className = `toast toast--${kind}`;
  toast.textContent = message;
  container.appendChild(toast);

  requestAnimationFrame(() => toast.classList.add("toast--visible"));

  setTimeout(() => {
    toast.classList.remove("toast--visible");
    setTimeout(() => toast.remove(), 250);
  }, 4000);
}

export function showError(err) {
  const message =
    err && err.detail
      ? typeof err.detail === "string"
        ? err.detail
        : JSON.stringify(err.detail)
      : err && err.message
      ? err.message
      : "Ocurrió un error inesperado";
  showToast(message, "error");
}

// ════════════════════════════════════════════════════════════════════════
// Modal de confirmación
// ════════════════════════════════════════════════════════════════════════
/**
 * Muestra un modal de confirmación y devuelve una Promise<boolean>.
 */
export function confirmDialog({
  title = "¿Estás seguro?",
  message = "",
  confirmLabel = "Confirmar",
  cancelLabel = "Cancelar",
  danger = false,
} = {}) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <div class="modal">
        <h3 class="modal-title">${title}</h3>
        <p class="modal-message">${message}</p>
        <div class="modal-actions">
          <button class="btn btn-ghost" data-action="cancel">${cancelLabel}</button>
          <button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-action="confirm">
            ${confirmLabel}
          </button>
        </div>
      </div>`;

    function close(result) {
      overlay.classList.remove("modal-overlay--visible");
      setTimeout(() => overlay.remove(), 150);
      resolve(result);
    }

    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close(false);
    });
    overlay
      .querySelector('[data-action="cancel"]')
      .addEventListener("click", () => close(false));
    overlay
      .querySelector('[data-action="confirm"]')
      .addEventListener("click", () => close(true));

    document.body.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add("modal-overlay--visible"));
  });
}

/**
 * Modal genérico de contenido libre (para formularios, p.ej. crear usuario).
 * `renderContent(bodyEl, close)` recibe el contenedor del cuerpo del modal
 * y una función close(result) para cerrarlo desde dentro.
 */
export function openModal({ title, renderContent, wide = false }) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <div class="modal ${wide ? "modal--wide" : ""}">
        <div class="modal-header">
          <h3 class="modal-title">${title}</h3>
          <button class="modal-close" aria-label="Cerrar">&times;</button>
        </div>
        <div class="modal-body"></div>
      </div>`;

    function close(result) {
      overlay.classList.remove("modal-overlay--visible");
      setTimeout(() => overlay.remove(), 150);
      resolve(result);
    }

    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close(undefined);
    });
    overlay.querySelector(".modal-close").addEventListener("click", () => close(undefined));

    const body = overlay.querySelector(".modal-body");
    renderContent(body, close);

    document.body.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add("modal-overlay--visible"));
  });
}

// ════════════════════════════════════════════════════════════════════════
// Helper de creación de elementos
// ════════════════════════════════════════════════════════════════════════
/**
 * h("div", {class:"foo", onclick: fn}, "texto", childEl, ...)
 * Pequeño helper estilo hyperscript para no escribir createElement repetido.
 */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "class") {
      el.className = value;
    } else if (key === "dataset") {
      Object.assign(el.dataset, value);
    } else if (value !== undefined && value !== null) {
      el.setAttribute(key, value);
    }
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function statusBadge(status) {
  const normalized = (status || "unknown").toLowerCase();
  const map = {
    active: "badge--success",
    running: "badge--success",
    up: "badge--success",
    stopped: "badge--neutral",
    down: "badge--danger",
    error: "badge--danger",
    pending: "badge--warning",
    building: "badge--warning",
  };
  const cls = map[normalized] || "badge--neutral";
  return h("span", { class: `badge ${cls}` }, status || "—");
}

export function roleBadge(role) {
  const map = {
    admin: "badge--admin",
    profesor: "badge--profesor",
    coach: "badge--coach",
    alumno: "badge--alumno",
  };
  return h("span", { class: `badge ${map[role] || "badge--neutral"}` }, role);
}
