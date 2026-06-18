import { AuthApi } from "../lib/api.js";
import { saveSession, isAuthenticated } from "../lib/auth.js";

// Si ya hay sesión activa, saltamos directo al dashboard.
if (isAuthenticated()) {
  window.location.href = "/";
}

const form = document.getElementById("login-form");
const errorBox = document.getElementById("login-error");
const submitBtn = document.getElementById("login-submit");

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.add("visible");
}

function hideError() {
  errorBox.classList.remove("visible");
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  hideError();

  const username = document.getElementById("username").value.trim();
  const password = document.getElementById("password").value;

  if (!username || !password) {
    showError("Usuario y contraseña son requeridos");
    return;
  }

  submitBtn.disabled = true;
  submitBtn.textContent = "Ingresando…";

  try {
    const response = await AuthApi.login(username, password);
    saveSession(response.access_token, response.user);
    window.location.href = "/";
  } catch (err) {
    const detail =
      err && err.detail
        ? typeof err.detail === "string"
          ? err.detail
          : "Credenciales inválidas"
        : "No se pudo conectar con el servidor";
    showError(detail);
    submitBtn.disabled = false;
    submitBtn.textContent = "Ingresar";
  }
});
