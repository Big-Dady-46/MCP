/* NexusAI — minimal vanilla JS (no frameworks) */

(function () {
  "use strict";

  // Mobile navigation toggle
  var toggle = document.getElementById("nav-toggle");
  var sidebar = document.getElementById("sidebar");
  if (toggle && sidebar) {
    toggle.addEventListener("click", function () {
      var open = sidebar.classList.toggle("is-open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("click", function (e) {
      if (
        sidebar.classList.contains("is-open") &&
        !sidebar.contains(e.target) &&
        !toggle.contains(e.target)
      ) {
        sidebar.classList.remove("is-open");
        toggle.setAttribute("aria-expanded", "false");
      }
    });
  }

  // Destructive actions ask for confirmation before submitting
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirm"))) {
        e.preventDefault();
      }
    });
  });

  // Auto-hide success alerts after a few seconds
  window.setTimeout(function () {
    document.querySelectorAll(".alert-success").forEach(function (el) {
      el.style.transition = "opacity .4s ease";
      el.style.opacity = "0";
      window.setTimeout(function () { el.remove(); }, 450);
    });
  }, 4000);
})();
