(function () {
  const request = async (service, action) => {
    const result = await window.App.fetchJson(`/${action}/${service}`, {
      method: "POST",
      toastOnError: false,
    });
    return result.data;
  };

  const refreshStatuses = async () => {
    try {
      const result = await window.App.fetchJson("/status", { toastOnError: false });
      const statuses = result.data || [];
      statuses.forEach((entry) => {
        const card = document.querySelector(`[data-service="${entry.key}"]`);
        if (!card) return;
        const pill = card.querySelector('[data-role="status"]');
        const uptime = card.querySelector('[data-role="uptime"]');
        if (entry.running) {
          pill.textContent = "En cours";
          pill.classList.add("running");
          pill.classList.remove("stopped");
          uptime.textContent = entry.uptime ? `${entry.uptime}s` : "—";
        } else {
          pill.textContent = "Arrêté";
          pill.classList.remove("running");
          pill.classList.add("stopped");
          uptime.textContent = "—";
        }
      });
    } catch (error) {
      console.error("Impossible de récupérer le statut", error);
    }
  };

  document.querySelectorAll(".service-card").forEach((card) => {
    const key = card.dataset.service;
    card.querySelectorAll("button[data-action]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const action = btn.dataset.action;
        btn.disabled = true;
        try {
          const payload = await request(key, action);
          if (payload && payload.ok) {
            window.App.showToast(
              action === "start" ? "Service démarré" : "Service arrêté",
              payload.message || "",
              "success"
            );
          } else {
            window.App.showToast(
              "Action refusée",
              (payload && payload.message) || "Impossible d'exécuter l'action.",
              "error"
            );
          }
          await refreshStatuses();
        } catch (err) {
          window.App.showToast(
            "Erreur",
            err.message || "Échec de la requête",
            "error"
          );
          console.error(err);
        } finally {
          btn.disabled = false;
        }
      });
    });
  });

  refreshStatuses();
  let pollTimer = setInterval(() => {
    if (document.visibilityState === "visible") {
      refreshStatuses();
    }
  }, 4000);

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      refreshStatuses();
    }
  });

  window.addEventListener("beforeunload", () => {
    clearInterval(pollTimer);
  });
})();
