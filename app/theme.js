// Modo día / noche. Se carga antes que la hoja de estilos para aplicar la elección
// guardada sin parpadeo; sin elección guardada se sigue la preferencia del sistema.
(function () {
  "use strict";
  const root = document.documentElement;
  const storageKey = "eclipse-planner-theme";
  const media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  let saved = null;
  try {
    saved = window.localStorage.getItem(storageKey);
  } catch (error) {
    saved = null;
  }
  if (saved === "light" || saved === "dark") root.dataset.theme = saved;

  function current() {
    return root.dataset.theme || (media && media.matches ? "dark" : "light");
  }

  function sync(button) {
    const dark = current() === "dark";
    const label = button.querySelector(".theme-label");
    if (label) label.textContent = dark ? "Modo día" : "Modo noche";
    button.setAttribute("aria-label", dark ? "Cambiar a modo día" : "Cambiar a modo noche");
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", dark ? "#0f1a1f" : "#122c36");
  }

  document.addEventListener("DOMContentLoaded", function () {
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    button.hidden = false;
    sync(button);
    button.addEventListener("click", function () {
      const next = current() === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try {
        window.localStorage.setItem(storageKey, next);
      } catch (error) {
        // Sin almacenamiento local, el cambio dura hasta cerrar la página.
      }
      sync(button);
    });
    if (media && media.addEventListener) media.addEventListener("change", function () { sync(button); });
  });
})();
