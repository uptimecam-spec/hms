(function () {
  var KEY = "camTheme";

  function current() {
    return document.documentElement.getAttribute("data-theme") || "dark";
  }

  function paintButton(theme) {
    var btn = document.getElementById("themeToggle");
    if (!btn) return;
    var next = theme === "dark" ? "light" : "dark";
    var dark = theme === "dark";
    btn.textContent = dark ? "\u2600 Light" : "\u263E Dark";
    if (btn.classList.contains("theme-toggle--float")) {
      btn.textContent = dark ? "\u2600" : "\u263E";
    }
    btn.title = "Switch to " + next + " theme";
    btn.setAttribute("aria-label", btn.title);
  }

  function apply(theme, persist) {
    document.documentElement.setAttribute("data-theme", theme);
    if (persist) {
      try { localStorage.setItem(KEY, theme); } catch (err) { /* private mode */ }
    }
    paintButton(theme);
  }

  function bind(btn) {
    if (!btn || btn.dataset.themeBound === "1") return;
    btn.dataset.themeBound = "1";
    btn.addEventListener("click", function () {
      apply(current() === "dark" ? "light" : "dark", true);
    });
  }

  function mount() {
    if (!document.body) return;
    var btn = document.getElementById("themeToggle");
    if (!btn) {
      // Login / pages without the app sidebar: keep a corner control.
      btn = document.createElement("button");
      btn.type = "button";
      btn.id = "themeToggle";
      btn.className = "theme-toggle theme-toggle--float";
      document.body.appendChild(btn);
    }
    bind(btn);
    paintButton(current());
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
})();
