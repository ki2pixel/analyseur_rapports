(function () {
  const tabs = document.querySelectorAll("[data-inspector-tabs] [data-filter]");
  const cards = document.querySelectorAll("[data-inspector-list] [data-compliance]");
  if (!tabs.length || !cards.length) return;

  function applyFilter(filter) {
    cards.forEach(function (card) {
      const compliance = card.getAttribute("data-compliance");
      card.hidden = filter !== "all" && compliance !== filter;
    });

    tabs.forEach(function (tab) {
      const active = tab.getAttribute("data-filter") === filter;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
  }

  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      applyFilter(tab.getAttribute("data-filter"));
    });
  });
})();
