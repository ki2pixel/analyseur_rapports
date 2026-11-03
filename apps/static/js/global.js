window.App = {
  /**
   * Affiche une notification toast
   * @param {string} title - Titre du toast
   * @param {string} message - Message optionnel
   * @param {string} type - 'success', 'error', ou 'info'
   * @param {number} duration - Durée en ms avant disparition (défaut 5000)
   */
  showToast: function (title, message = "", type = "info", duration = 5000) {
    const container = document.getElementById("toast-container");
    if (!container) return;

    const toast = document.createElement("div");
    toast.className = `toast toast--${type}`;

    const titleEl = document.createElement("div");
    titleEl.className = "toast__title";
    titleEl.textContent = title;
    toast.appendChild(titleEl);

    if (message) {
      const msgEl = document.createElement("div");
      msgEl.className = "toast__message";
      msgEl.textContent = message;
      toast.appendChild(msgEl);
    }

    container.appendChild(toast);

    setTimeout(() => {
      toast.classList.add("toast--closing");
      toast.addEventListener("animationend", () => {
        toast.remove();
      });
    }, duration);
  },

  getCsrfToken: function () {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  },

  authHeaders: function (extra = {}) {
    const headers = Object.assign({}, extra);
    const csrf = window.App.getCsrfToken();
    if (csrf) {
      headers["X-CSRFToken"] = csrf;
    }
    return headers;
  },

  /**
   * fetch + JSON parse + CSRF + toast d'erreur générique.
   * @returns {Promise<{ok: boolean, status: number, data: any}>}
   */
  fetchJson: async function (url, options = {}) {
    const opts = Object.assign({}, options);
    const method = (opts.method || "GET").toUpperCase();
    const headers = window.App.authHeaders(opts.headers || {});
    opts.headers = headers;

    let response;
    try {
      response = await fetch(url, opts);
    } catch (err) {
      const message = err && err.message ? err.message : "Erreur réseau";
      if (window.App.showToast) {
        window.App.showToast("Erreur réseau", message, "error");
      }
      throw err;
    }

    let data = null;
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      try {
        data = await response.json();
      } catch (err) {
        const message = "Réponse JSON invalide";
        if (window.App.showToast && method !== "GET") {
          window.App.showToast("Erreur", message, "error");
        }
        const error = new Error(message);
        error.response = response;
        throw error;
      }
    } else {
      try {
        data = await response.json();
      } catch (_err) {
        data = null;
      }
    }

    if (!response.ok) {
      const message =
        (data && (data.error || data.message)) ||
        `Erreur HTTP ${response.status}`;
      if (window.App.showToast && options.toastOnError !== false) {
        window.App.showToast("Erreur", message, "error");
      }
      const error = new Error(message);
      error.response = response;
      error.data = data;
      throw error;
    }

    return { ok: true, status: response.status, data: data };
  },

  initMobileNav: function () {
    const topbar = document.getElementById("topbar");
    const toggle = document.getElementById("navToggle");
    const links = document.getElementById("topbar-links");
    const backdrop = document.getElementById("navBackdrop");
    if (!topbar || !toggle || !links) return;

    function setOpen(open) {
      topbar.classList.toggle("is-open", open);
      document.body.classList.toggle("nav-open", open);
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      toggle.setAttribute("aria-label", open ? "Fermer le menu" : "Ouvrir le menu");
      if (backdrop) {
        if (open) {
          backdrop.removeAttribute("hidden");
        } else {
          backdrop.setAttribute("hidden", "");
        }
      }
    }

    function close() {
      setOpen(false);
    }

    toggle.addEventListener("click", function () {
      const open = toggle.getAttribute("aria-expanded") === "true";
      setOpen(!open);
    });

    if (backdrop) {
      backdrop.addEventListener("click", close);
    }

    links.querySelectorAll("a").forEach(function (link) {
      link.addEventListener("click", close);
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        close();
      }
    });
  },

  initMonthSelector: function () {
    const form = document.querySelector("[data-month-form]");
    if (!form) return;
    const input = form.querySelector('input[type="month"]');
    if (!input) return;
    input.addEventListener("change", function () {
      if (input.value) {
        form.submit();
      }
    });
  },
};

document.addEventListener("DOMContentLoaded", function () {
  if (window.App && window.App.initMobileNav) {
    window.App.initMobileNav();
  }
  if (window.App && window.App.initMonthSelector) {
    window.App.initMonthSelector();
  }
});
