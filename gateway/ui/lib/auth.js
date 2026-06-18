/**
 * lib/auth.js
 *
 * Maneja la sesión del usuario en el cliente:
 *   - Guarda/lee el JWT y los datos públicos del usuario (in-memory + sessionStorage
 *     NO se usa — usamos un objeto JS module-level + persistencia manual vía un
 *     pequeño wrapper, ya que las reglas del entorno prohíben localStorage en
 *     artifacts. Aquí SÍ estamos en gateway/ui servido como archivos estáticos
 *     reales por FastAPI, no en un artifact de Claude — así que sessionStorage
 *     funciona normal en el navegador del usuario final.)
 *   - Decodifica claims del JWT (rol, cursos, etc.) sin librería externa.
 *   - Expone helpers de autorización usados por el router y las páginas.
 */

const TOKEN_KEY = "pucp_token";
const USER_KEY = "pucp_user";

/** Decodifica el payload de un JWT (sin verificar firma — eso lo hace el backend). */
function decodeJwtPayload(token) {
  try {
    const [, payloadB64] = token.split(".");
    let base64 = payloadB64.replace(/-/g, "+").replace(/_/g, "/");
    // atob() exige longitud múltiplo de 4 — los JWT en base64url normalmente
    // omiten el padding '=', así que lo restituimos antes de decodificar.
    const paddingNeeded = (4 - (base64.length % 4)) % 4;
    base64 += "=".repeat(paddingNeeded);
    const json = atob(base64);
    return JSON.parse(decodeURIComponent(escape(json)));
  } catch (err) {
    console.error("Token JWT malformado", err);
    return null;
  }
}

export function saveSession(token, userPublic) {
  sessionStorage.setItem(TOKEN_KEY, token);
  sessionStorage.setItem(USER_KEY, JSON.stringify(userPublic));
}

export function clearSession() {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(USER_KEY);
}

export function getToken() {
  return sessionStorage.getItem(TOKEN_KEY);
}

/** Usuario "público" tal como lo devuelve /auth/login o /auth/me. */
export function getUser() {
  const raw = sessionStorage.getItem(USER_KEY);
  return raw ? JSON.parse(raw) : null;
}

/** ¿Hay una sesión activa con un token no expirado? */
export function isAuthenticated() {
  const token = getToken();
  if (!token) return false;
  const payload = decodeJwtPayload(token);
  if (!payload || !payload.exp) return false;
  const nowSeconds = Date.now() / 1000;
  return payload.exp > nowSeconds;
}

export function getRole() {
  const user = getUser();
  return user ? user.rol : null;
}

export function getCourses() {
  const user = getUser();
  return user ? user.courses || [] : [];
}

export function hasRole(...roles) {
  const role = getRole();
  return role !== null && roles.includes(role);
}

export function isAdmin() {
  return hasRole("admin");
}

export function isProfesor() {
  return hasRole("profesor");
}

export function isCoach() {
  return hasRole("coach");
}

export function isAlumno() {
  return hasRole("alumno");
}

/** Roles que NO pueden mutar nada (solo lectura) — hoy: coach. */
export function isReadOnly() {
  return isCoach();
}

/** Roles que pueden crear/editar/borrar (todo menos coach). */
export function canWrite() {
  return !isReadOnly();
}

/** Roles que pueden crear slices "on behalf of" otro usuario. */
export function canActOnBehalf() {
  return hasRole("admin", "profesor");
}

/** Roles que pueden administrar usuarios del sistema. */
export function canManageUsers() {
  return hasRole("admin");
}

/** Roles que pueden administrar cursos (crear/listar/editar el propio). */
export function canManageCourses() {
  return hasRole("admin", "profesor");
}

export function logout() {
  clearSession();
  window.location.href = "/login.html";
}

/**
 * Si no hay sesión válida, redirige a login y devuelve false.
 * Las páginas protegidas llaman esto al iniciar.
 */
export function requireAuth() {
  if (!isAuthenticated()) {
    window.location.href = "/login.html";
    return false;
  }
  return true;
}
