(function () {
  const btn = document.getElementById("toggleSummary");
  const content = document.getElementById("summaryContent");
  const chevron = btn ? btn.querySelector(".accordion__chevron, .chevron") : null;
  const monthMeta = document.querySelector('meta[name="selected-month"]');
  const month = monthMeta ? monthMeta.getAttribute("content") : "";
  if (btn && content && month) {
    const key = "summaryOpen:" + month;
    function setOpen(open) {
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      content.classList.toggle("is-open", open);
      content.style.maxHeight = open ? "600px" : "0";
      if (chevron) {
        chevron.classList.toggle("is-open", open);
      }
    }
    const saved = localStorage.getItem(key);
    const initOpen = saved ? saved === "1" : false;
    setOpen(initOpen);
    btn.addEventListener("click", function () {
      const openNow = btn.getAttribute("aria-expanded") === "true";
      const next = !openNow;
      setOpen(next);
      localStorage.setItem(key, next ? "1" : "0");
    });
  }

  const generateBtn = document.getElementById("generate-json-btn");
  if (generateBtn) {
    generateBtn.addEventListener("click", async function () {
      const selectedMonth = generateBtn.getAttribute("data-month");
      if (!selectedMonth) return;
      generateBtn.disabled = true;
      const oldText = generateBtn.textContent;
      generateBtn.textContent = "Génération...";
      try {
        const result = await window.App.fetchJson(
          `/generate-json?month=${encodeURIComponent(selectedMonth)}`,
          { method: "POST", toastOnError: false }
        );
        const data = result.data || {};
        if (data.status === "success") {
          window.App.showToast(
            "Succès",
            `Rapport JSON généré sous apps/reporting/${data.file}`,
            "success"
          );
        } else {
          window.App.showToast(
            "Erreur",
            data.message || data.error || "Une erreur est survenue.",
            "error"
          );
        }
      } catch (err) {
        window.App.showToast(
          "Erreur",
          err.message || "Erreur lors de la requête de génération.",
          "error"
        );
        console.error(err);
      } finally {
        generateBtn.disabled = false;
        generateBtn.textContent = oldText;
      }
    });
  }
})();
