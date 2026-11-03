(function () {
  const btn = document.getElementById("convertBtn");
  if (!btn) return;

  const actionsBox = document.getElementById("actions");
  const reportLink = document.getElementById("reportLink");
  const reportLinkLabel = document.getElementById("reportLinkLabel");
  const progress = document.getElementById("progress");
  const progressBar = document.getElementById("progressBar");
  const progressLabel = document.getElementById("progressLabel");
  const progressLog = document.getElementById("progressLog");
  const documentsBox = document.getElementById("documentCards");
  const overlapBox = document.getElementById("overlapAlerts");
  const batchWarningsBox = document.getElementById("batchWarnings");
  let inFlight = false;

  function resetProgressLog() {
    if (!progressLog) return;
    progressLog.replaceChildren();
    progressLog.classList.add("is-hidden");
  }

  function setBusy(busy) {
    if (!progress) return;
    progress.classList.toggle("progress--indeterminate", busy);
    progress.setAttribute("aria-busy", busy ? "true" : "false");
    if (progressBar) {
      if (busy) {
        progressBar.removeAttribute("aria-valuenow");
      } else {
        progressBar.setAttribute("aria-valuenow", "0");
      }
    }
  }

  function finishProgress(success) {
    setBusy(false);
    if (progressBar) {
      progressBar.style.width = success ? "100%" : "0%";
      progressBar.setAttribute("aria-valuenow", success ? "100" : "0");
    }
    if (progressLabel) {
      progressLabel.textContent = success ? "Conversion terminée" : "Erreur";
    }
  }

  function clearResults() {
    if (documentsBox) documentsBox.replaceChildren();
    if (overlapBox) overlapBox.replaceChildren();
    if (batchWarningsBox) batchWarningsBox.replaceChildren();
  }

  function badge(text, modifier) {
    const span = document.createElement("span");
    span.className = `badge ${modifier}`;
    span.textContent = text;
    return span;
  }

  function renderOverlaps(overlaps) {
    if (!overlapBox) return;
    overlapBox.replaceChildren();
    if (!Array.isArray(overlaps) || !overlaps.length) return;

    const panel = document.createElement("div");
    panel.className = "panel panel--warn";
    const title = document.createElement("h3");
    title.textContent = "Chevauchements de BDC détectés";
    panel.appendChild(title);

    const intro = document.createElement("p");
    intro.className = "muted";
    intro.textContent =
      "Les lots suivants sont revendiqués par plusieurs bons de commande du même import.";
    panel.appendChild(intro);

    const list = document.createElement("ul");
    overlaps.forEach(function (overlap) {
      const item = document.createElement("li");
      item.textContent = `Lot ${overlap.lot} — ${(overlap.bdc_numbers || []).join(", ")}`;
      list.appendChild(item);
    });
    panel.appendChild(list);
    overlapBox.appendChild(panel);
  }

  function renderBatchWarnings(warnings) {
    if (!batchWarningsBox) return;
    batchWarningsBox.replaceChildren();
    if (!Array.isArray(warnings) || !warnings.length) return;

    const panel = document.createElement("div");
    panel.className = "panel panel--warn";
    const title = document.createElement("h3");
    title.textContent = "Avertissements du batch";
    panel.appendChild(title);

    const list = document.createElement("ul");
    warnings.forEach(function (warning) {
      const item = document.createElement("li");
      item.textContent = warning;
      list.appendChild(item);
    });
    panel.appendChild(list);
    batchWarningsBox.appendChild(panel);
  }

  function renderDocuments(documents, failedFiles) {
    if (!documentsBox) return;
    documentsBox.replaceChildren();

    (documents || []).forEach(function (doc) {
      const status = doc.status || "ok";
      const card = document.createElement("article");
      card.className = "doc-card";
      if (status === "partial") card.classList.add("doc-card--partial");
      if (status === "failed") card.classList.add("doc-card--failed");

      const head = document.createElement("div");
      head.className = "doc-card__head";

      const id = document.createElement("strong");
      id.className = "doc-card__id";
      id.textContent = doc.numero_bdc || "BDC non détecté";
      head.appendChild(id);

      const articles = document.createElement("span");
      articles.className = "badge badge--neutral";
      articles.textContent = `${doc.articles_count || 0} article(s)`;
      head.appendChild(articles);
      card.appendChild(head);

      const reference = document.createElement("p");
      reference.className = "doc-card__ref";
      reference.textContent = doc.reference || "Référence de lots non détectée";
      card.appendChild(reference);

      const filename = document.createElement("p");
      filename.className = "muted muted--tight";
      filename.textContent = doc.filename || "";
      card.appendChild(filename);

      const badges = document.createElement("div");
      badges.className = "doc-card__badges";
      const references = Array.isArray(doc.references) ? doc.references : [];
      const hasRange =
        String(doc.reference || "").includes(" à ") ||
        references.some(function (value) {
          return String(value).includes(" à ");
        });
      const layout = doc.layout && typeof doc.layout === "object" ? doc.layout : null;
      if (status === "failed") {
        badges.appendChild(badge("Échec extraction", "badge--danger"));
      } else if (status === "partial") {
        badges.appendChild(badge("Partiel", "badge--warn"));
      }
      if (layout && layout.changed) {
        badges.appendChild(badge("Structure modifiée", "badge--warn"));
      }
      if (hasRange) {
        badges.appendChild(badge("Plage détectée", "badge--warn"));
      }
      (doc.overlaps || []).forEach(function (overlap) {
        badges.appendChild(
          badge(
            `Chevauchement lot ${overlap.lot} (${(overlap.bdc_numbers || []).join(", ")})`,
            "badge--danger"
          )
        );
      });
      if (
        status === "ok" &&
        !hasRange &&
        !(layout && layout.changed) &&
        !(doc.overlaps || []).length
      ) {
        badges.appendChild(badge("✓ Intègre", "badge--ok"));
      }
      card.appendChild(badges);

      if (layout && layout.changed) {
        const layoutInfo = document.createElement("p");
        layoutInfo.className = "doc-card__layout";
        layoutInfo.textContent = layout.fingerprint
          ? `Structure détectée : ${layout.fingerprint}`
          : "Structure : en-tête de tableau non reconnu";
        if (layout.expected) {
          layoutInfo.title = `Structure attendue : ${layout.expected}`;
        }
        card.appendChild(layoutInfo);
      }

      const warnings = Array.isArray(doc.warnings) ? doc.warnings : [];
      if (warnings.length) {
        const list = document.createElement("ul");
        list.className = "doc-card__warnings";
        warnings.forEach(function (warning) {
          const item = document.createElement("li");
          item.textContent = warning;
          list.appendChild(item);
        });
        card.appendChild(list);
      }

      documentsBox.appendChild(card);
    });

    (failedFiles || []).forEach(function (failure) {
      const card = document.createElement("article");
      card.className = "doc-card doc-card--error";

      const head = document.createElement("div");
      head.className = "doc-card__head";
      const name = document.createElement("strong");
      name.className = "doc-card__id";
      name.textContent = failure.filename || "PDF inconnu";
      head.appendChild(name);
      head.appendChild(badge("Échec", "badge--danger"));
      card.appendChild(head);

      const message = document.createElement("p");
      message.className = "doc-card__ref";
      message.textContent = failure.error || "Erreur de lecture du PDF.";
      card.appendChild(message);

      documentsBox.appendChild(card);
    });
  }

  btn.addEventListener("click", async () => {
    if (inFlight) return;
    inFlight = true;
    btn.disabled = true;
    resetProgressLog();
    clearResults();
    actionsBox.classList.add("is-hidden");
    setBusy(true);
    progressLabel.textContent = "Traitement en cours…";
    if (progressBar) {
      progressBar.style.width = "";
    }

    try {
      const result = await window.App.fetchJson("/api/convert", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
        toastOnError: false,
      });
      const payload = result.data || {};
      const documents = Array.isArray(payload.documents) ? payload.documents : [];
      const failedFiles = Array.isArray(payload.failed_files) ? payload.failed_files : [];
      const failedDocuments = documents.filter(function (doc) {
        return (doc.status || "ok") === "failed";
      }).length;
      const partialDocuments = documents.filter(function (doc) {
        return (doc.status || "ok") === "partial";
      }).length;
      const layoutDriftCount = documents.filter(function (doc) {
        return doc.layout && doc.layout.changed;
      }).length;
      const failedCount = failedFiles.length + failedDocuments;
      const details = `Fichiers traités : ${payload.successful_files || 0}/${payload.processed_files || 0}`;
      const toastType = failedCount
        ? "error"
        : partialDocuments || layoutDriftCount
          ? "info"
          : "success";
      const toastTitle = failedCount
        ? "Conversion terminée avec des erreurs"
        : partialDocuments
          ? "Conversion terminée avec avertissements"
          : layoutDriftCount
            ? "Conversion terminée avec une structure modifiée"
            : payload.message || "Conversion terminée";
      window.App.showToast(toastTitle, details, toastType);

      renderDocuments(documents, failedFiles);
      renderOverlaps(payload.overlaps);
      renderBatchWarnings(payload.batch ? payload.batch.warnings : []);

      if (payload.batch && payload.batch.report_url) {
        actionsBox.classList.remove("is-hidden");
        reportLink.href = payload.batch.report_url;
        if (reportLinkLabel) {
          reportLinkLabel.textContent = `Ouvrir le rapport ${payload.batch.batch_id}`;
        }
      }

      if (Array.isArray(payload.progress) && payload.progress.length) {
        progressLog.classList.remove("is-hidden");
        payload.progress.forEach((step) => {
          const li = document.createElement("li");
          li.textContent = `${step.index}/${step.total} • ${step.filename} • ${step.percent}%`;
          progressLog.appendChild(li);
        });
      }

      finishProgress(true);
      if (progressLabel) {
        progressLabel.textContent = failedCount
          ? "Conversion terminée avec des erreurs"
          : partialDocuments
            ? "Conversion terminée avec avertissements"
            : layoutDriftCount
              ? "Conversion terminée avec une structure modifiée"
              : "Conversion terminée";
      }
    } catch (error) {
      const errorData = error && error.data ? error.data : {};
      renderDocuments(errorData.documents, errorData.failed_files);
      renderOverlaps(errorData.overlaps);
      renderBatchWarnings(errorData.batch ? errorData.batch.warnings : []);

      window.App.showToast(
        "Erreur de conversion",
        error.message || "Erreur inconnue",
        "error"
      );
      finishProgress(false);
      resetProgressLog();
      actionsBox.classList.add("is-hidden");
    } finally {
      btn.disabled = false;
      inFlight = false;
    }
  });
})();
