// Botón flotante para volver al principio de la página. Aparece tras bajar algo más de una
// pantalla; al pulsarlo sube (con animación si el sistema no pide reducir el movimiento) y deja
// el foco en el logotipo, para que el teclado y los lectores de pantalla también vuelvan arriba.
(function () {
  "use strict";
  document.addEventListener("DOMContentLoaded", function () {
    const button = document.getElementById("back-to-top");
    if (!button) return;
    const reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)");
    let ticking = false;

    function update() {
      ticking = false;
      const visible = window.scrollY > window.innerHeight * 1.2;
      button.classList.toggle("is-visible", visible);
      button.tabIndex = visible ? 0 : -1;
      button.setAttribute("aria-hidden", visible ? "false" : "true");
    }

    window.addEventListener("scroll", function () {
      if (!ticking) {
        ticking = true;
        window.requestAnimationFrame(update);
      }
    }, { passive: true });
    window.addEventListener("resize", update);

    button.addEventListener("click", function () {
      const smooth = !(reduceMotion && reduceMotion.matches);
      window.scrollTo({ top: 0, behavior: smooth ? "smooth" : "auto" });
      const brand = document.querySelector(".brand");
      if (brand) brand.focus({ preventScroll: true });
    });

    button.hidden = false;
    update();
  });
})();
