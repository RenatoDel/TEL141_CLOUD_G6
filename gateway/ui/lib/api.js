/**
 * lib/api.js
 *
 * Wrapper sobre fetch() que:
 *   - Agrega el header Authorization: Bearer <token> automáticamente.
 *   - Normaliza errores HTTP en un objeto ApiError consistente.
 *   - Centraliza las rutas del backend (todas pasan por el gateway).
 *
 * El gateway expone:
 *   /auth/*   → auth_service  (login, users, courses)
 *   /api/*    → slice_manager (graph-slices, monitoring)
 */

import { getToken, logout } from "./auth.js";

export class ApiError extends Error {
  constructor(status, detail, raw) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
    this.raw = raw;
  }
}

async function request(path, { method = "GET", body, headers = {} } = {}) {
  const token = getToken();
  const finalHeaders = {
    ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...headers,
  };

  let response;
  try {
    response = await fetch(path, {
      method,
      headers: finalHeaders,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (networkErr) {
    throw new ApiError(0, "No se pudo conectar con el servidor", networkErr);
  }

  // 401 global: la sesión expiró o el token es inválido → forzar logout.
  if (response.status === 401) {
    logout();
    throw new ApiError(401, "Sesión expirada", null);
  }

  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  if (!response.ok) {
    const detail = data && data.detail !== undefined ? data.detail : data;
    throw new ApiError(response.status, detail, data);
  }

  return data;
}

// ════════════════════════════════════════════════════════════════════════
// Auth / usuarios / cursos  (prefijo /auth)
// ════════════════════════════════════════════════════════════════════════
export const AuthApi = {
  login: (username, password) =>
    request("/auth/login", { method: "POST", body: { username, password } }),

  me: () => request("/auth/me"),

  listUsers: () => request("/auth/users"),
  createUser: (payload) =>
    request("/auth/users", { method: "POST", body: payload }),
  getUser: (username) => request(`/auth/users/${encodeURIComponent(username)}`),
  updateUser: (username, payload) =>
    request(`/auth/users/${encodeURIComponent(username)}`, {
      method: "PATCH",
      body: payload,
    }),
  deleteUser: (username) =>
    request(`/auth/users/${encodeURIComponent(username)}`, { method: "DELETE" }),

  listCourses: () => request("/auth/courses"),
  createCourse: (payload) =>
    request("/auth/courses", { method: "POST", body: payload }),
  getCourse: (codigo) => request(`/auth/courses/${encodeURIComponent(codigo)}`),
  updateCourse: (codigo, payload) =>
    request(`/auth/courses/${encodeURIComponent(codigo)}`, {
      method: "PATCH",
      body: payload,
    }),
  deleteCourse: (codigo) =>
    request(`/auth/courses/${encodeURIComponent(codigo)}`, { method: "DELETE" }),

  enrollStudents: (codigo, alumnoUsernames) =>
    request(`/auth/courses/${encodeURIComponent(codigo)}/members`, {
      method: "POST",
      body: { alumno_usernames: alumnoUsernames },
    }),
  unenrollStudent: (codigo, username) =>
    request(
      `/auth/courses/${encodeURIComponent(codigo)}/members/${encodeURIComponent(username)}`,
      { method: "DELETE" }
    ),

  // ── Coaches (M:N curso ↔ coach, admin-only para asignar) ─────────────
  assignCoaches: (codigo, coachUsernames) =>
    request(`/auth/courses/${encodeURIComponent(codigo)}/coaches`, {
      method: "POST",
      body: { coach_usernames: coachUsernames },
    }),
  removeCoach: (codigo, username) =>
    request(
      `/auth/courses/${encodeURIComponent(codigo)}/coaches/${encodeURIComponent(username)}`,
      { method: "DELETE" }
    ),

  // ── Listados públicos (no requieren admin) ───────────────────────────
  listStudents: () => request("/auth/students-listable"),
  listCoaches: () => request("/auth/coaches-listable"),
};

// ════════════════════════════════════════════════════════════════════════
// Imágenes  (prefijo /api/images → image_service vía gateway)
// ════════════════════════════════════════════════════════════════════════
export const ImageApi = {
  list: () => request("/api/images"),

  // Importa desde una URL http(s): el image_service la descarga por su cuenta
  // (no pasa por el navegador ni por el disco del cliente). Usa Form fields.
  importUrl: async ({ name, url, os_type = "linux", format = "qcow2" }) => {
    const form = new FormData();
    form.append("name", name);
    form.append("url", url);
    form.append("os_type", os_type);
    form.append("format", format);
    return _formRequest("/api/images/import-url", { method: "POST", body: form });
  },

  // Sube un archivo local (multipart) al image_service.
  upload: async ({ name, file, os_type = "linux", format = "qcow2" }) => {
    const form = new FormData();
    form.append("name", name);
    form.append("os_type", os_type);
    form.append("format", format);
    form.append("file", file);
    return _formRequest("/api/images/upload", { method: "POST", body: form });
  },

  remove: (name) =>
    request(`/api/images/${encodeURIComponent(name)}`, { method: "DELETE" }),
};

// Igual que request() pero para FormData (no fija Content-Type: el navegador
// pone el boundary multipart solo). Reutiliza el mismo manejo de errores/401.
async function _formRequest(path, { method = "POST", body } = {}) {
  const token = getToken();
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body,
    });
  } catch (networkErr) {
    throw new ApiError(0, "No se pudo conectar con el servidor", networkErr);
  }
  if (response.status === 401) {
    logout();
    throw new ApiError(401, "Sesión expirada", null);
  }
  const text = await response.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }
  if (!response.ok) {
    const detail = data && data.detail !== undefined ? data.detail : data;
    throw new ApiError(response.status, detail, data);
  }
  return data;
}

// ════════════════════════════════════════════════════════════════════════
// Slices / monitoreo  (prefijo /api → slice_manager)
// ════════════════════════════════════════════════════════════════════════
export const SliceApi = {
  listGraphSlices: () => request("/api/graph-slices"),

  createGraphSlice: (payload) =>
    request("/api/graph-slices", { method: "POST", body: payload }),

  createDraft: (payload) =>
    request("/api/graph-slices/drafts", { method: "POST", body: payload }),

  updateDraft: (sliceName, payload) =>
    request(`/api/graph-slices/drafts/${encodeURIComponent(sliceName)}`, {
      method: "PUT",
      body: payload,
    }),

  deployDraft: (sliceName) =>
    request(`/api/graph-slices/drafts/${encodeURIComponent(sliceName)}/deploy`, {
      method: "POST",
    }),

  exportGraphSlice: (sliceName) =>
    request(`/api/graph-slices/${encodeURIComponent(sliceName)}/export`),

  importGraphSlice: (topology, newSliceName = null) =>
    request("/api/graph-slices/import", {
      method: "POST",
      body: {
        topology,
        ...(newSliceName ? { new_slice_name: newSliceName } : {}),
      },
    }),

  cloneGraphSlice: (sliceName, newSliceName) =>
    request(`/api/graph-slices/${encodeURIComponent(sliceName)}/clone`, {
      method: "POST",
      body: { new_slice_name: newSliceName },
    }),

  deleteGraphSlice: (sliceName) =>
    request(`/api/graph-slices/${encodeURIComponent(sliceName)}`, {
      method: "DELETE",
    }),

  vmAction: (sliceName, vmName, action) =>
    request(
      `/api/graph-vms/${encodeURIComponent(sliceName)}/${encodeURIComponent(vmName)}/action`,
      { method: "POST", body: { action } }
    ),

  /** Obtiene URL de consola fresca desde Nova (token nuevo cada vez). */
  getVmConsole: (sliceName, vmName) =>
    request(
      `/api/graph-vms/${encodeURIComponent(sliceName)}/${encodeURIComponent(vmName)}/console`
    ),

  monitoringSummary: () => request("/api/monitoring/summary"),

  /** Resumen por curso (para profesor/coach) — slices visibles agrupados. */
  monitoringCoursesSummary: () => request("/api/monitoring/courses-summary"),

  // ── Polling de jobs (módulo de colas Redis + RQ) ──────────────────────
  /** Consulta el estado actual del job de deploy/borrado de un slice. */
  getJobStatus: (sliceName) =>
    request(`/api/graph-slices/${encodeURIComponent(sliceName)}/job-status`),

  placementWorkersStatus: (zone) =>
  request(`/api/placement/workers/status${zone ? `?zone=${zone}` : ""}`),
  /**
   * Hace polling de getJobStatus hasta que el job termine (finished/active)
   * o falle, o se agote maxAttempts. Llama a onUpdate en cada tick con el
   * estado actual, para que la UI pueda actualizar un badge en vivo.
   */
  async pollUntilDone(sliceName, { intervalMs = 2500, maxAttempts = 80, onUpdate } = {}) {
    for (let i = 0; i < maxAttempts; i++) {
      const status = await this.getJobStatus(sliceName);
      if (onUpdate) onUpdate(status);

      if (status.status === "finished" || status.status === "active") return status;
      // "not_found" significa que el job expiró del TTL de Redis Y el slice
      // ya no está en state_store — para un borrado, eso es éxito.
      if (status.status === "not_found") return status;
      if (status.status === "failed") throw new ApiError(0, status.error || "El job falló", status);
      await new Promise((r) => setTimeout(r, intervalMs));
    }
    throw new ApiError(0, "Tiempo de espera agotado consultando el estado del job", null);
  },
};

/**
 * Construye la URL del websocket de consola VNC para una VM.
 * El gateway espera: /ws/vnc-proxy?worker=X&port=Y&token=Z
 */
export function buildVncWsUrl(worker, port) {
  const token = getToken();
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const host = window.location.host;
  return `${proto}://${host}/ws/vnc-proxy?worker=${encodeURIComponent(
    worker
  )}&port=${encodeURIComponent(port)}&token=${encodeURIComponent(token)}`;
}
