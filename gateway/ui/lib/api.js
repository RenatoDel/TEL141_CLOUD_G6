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
// Slices / monitoreo  (prefijo /api → slice_manager)
// ════════════════════════════════════════════════════════════════════════
export const SliceApi = {
  listGraphSlices: () => request("/api/graph-slices"),

  createGraphSlice: (payload) =>
    request("/api/graph-slices", { method: "POST", body: payload }),

  deleteGraphSlice: (sliceName) =>
    request(`/api/graph-slices/${encodeURIComponent(sliceName)}`, {
      method: "DELETE",
    }),

  vmAction: (sliceName, vmName, action) =>
    request(
      `/api/graph-vms/${encodeURIComponent(sliceName)}/${encodeURIComponent(vmName)}/action`,
      { method: "POST", body: { action } }
    ),

  monitoringSummary: () => request("/api/monitoring/summary"),

  /** Resumen por curso (para profesor/coach) — slices visibles agrupados. */
  monitoringCoursesSummary: () => request("/api/monitoring/courses-summary"),
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
