const CloudUI = (() => {
  const TOKEN_KEY = "pucp_token";
  const USER_KEY = "pucp_user";

  function getToken() {
    return localStorage.getItem(TOKEN_KEY) || "";
  }

  function getUser() {
    const raw = localStorage.getItem(USER_KEY);
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch {
      return null;
    }
  }

  function saveSession(data) {
    localStorage.setItem(TOKEN_KEY, data.access_token || "");
    localStorage.setItem(USER_KEY, JSON.stringify(data.user || {}));
  }

  function clearSession() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
  }

  function requireAuth() {
    if (!getToken()) {
      window.location.href = "/login.html";
      return false;
    }
    return true;
  }

  function setupLayout(pageKey) {
    const navItems = document.querySelectorAll("[data-nav]");
    navItems.forEach((item) => {
      if (item.dataset.nav === pageKey) item.classList.add("active");
    });

    const user = getUser();
    document.querySelectorAll("[data-user-name]").forEach((el) => {
      el.textContent = user?.username || "admin";
    });

    document.querySelectorAll("[data-logout]").forEach((btn) => {
      btn.addEventListener("click", () => {
        clearSession();
        window.location.href = "/login.html";
      });
    });
  }

  async function api(path, options = {}) {
    const headers = {
      ...(options.headers || {}),
    };

    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;

    const response = await fetch(path, {
      ...options,
      headers,
    });

    const text = await response.text();
    let data;
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = text;
    }

    if (!response.ok) {
      const detail = typeof data === "object" ? (data.detail || JSON.stringify(data)) : data;
      throw new Error(detail || `HTTP ${response.status}`);
    }

    return data;
  }

  function showResponse(targetId, data) {
    const el = document.getElementById(targetId);
    if (!el) return;
    el.textContent = typeof data === "string" ? data : JSON.stringify(data, null, 2);
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function stat(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  return {
    getToken,
    getUser,
    saveSession,
    clearSession,
    requireAuth,
    setupLayout,
    api,
    showResponse,
    escapeHtml,
    stat,
  };
})();
