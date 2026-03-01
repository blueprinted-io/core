(function () {
  const overlay = document.getElementById("info-overlay");
  if (!overlay) return;

  const titleEl = document.getElementById("info-overlay-title");
  const bodyEl = document.getElementById("info-overlay-body");
  const closeBtn = document.getElementById("info-overlay-close");

  function closeOverlay() {
    overlay.classList.remove("active");
    overlay.setAttribute("hidden", "");
  }

  function openOverlay(title, content) {
    if (titleEl) titleEl.textContent = title || "";
    if (bodyEl) bodyEl.textContent = content || "";
    overlay.removeAttribute("hidden");
    overlay.classList.add("active");
    if (closeBtn instanceof HTMLElement) closeBtn.focus();
  }

  document.addEventListener("click", function (event) {
    const target = event.target;
    if (!(target instanceof Element)) return;

    const trigger = target.closest(".js-info-btn");
    if (trigger) {
      const title = trigger.getAttribute("data-info-title") || "";
      const content = trigger.getAttribute("data-info-content") || "";
      openOverlay(title, content);
      return;
    }

    if (target === overlay || target.closest("#info-overlay-close")) {
      closeOverlay();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !overlay.hasAttribute("hidden")) {
      closeOverlay();
    }
  });
})();
