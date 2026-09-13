/**
 * Controles de interfaz de SAMY Cloud, sin evaluacion de cadenas.
 *
 * POR QUE EXISTE ESTE ARCHIVO
 * ---------------------------
 * La CSP de produccion es `script-src 'self' https://pay.conekta.com`: sin
 * 'unsafe-inline' y **sin 'unsafe-eval'**. Alpine.js evalua sus expresiones
 * (`x-show="open"`, `:type="show ? 'text' : 'password'"`) construyendo
 * funciones a partir de cadenas, y eso es exactamente lo que 'unsafe-eval'
 * habilita. Sin esa palabra, el navegador lanza
 *
 *     EvalError: Evaluating a string as JavaScript violates the following
 *     Content Security Policy directive: script-src 'self' ...
 *
 * en CADA directiva. Y aqui esta lo peligroso: la pantalla se pinta completa.
 * No hay error visible, no hay hueco en el diseno, no hay nada que el cajero
 * pueda notar. Simplemente los botones no hacen nada.
 *
 * Se verifico en el navegador el 13/09/2026 con CSP_ENFORCE=True: las seis
 * directivas de la pantalla de login lanzaron EvalError y el boton de
 * mostrar/ocultar contrasena quedo muerto.
 *
 * La alternativa era el build `@alpinejs/csp`, y se descarto: ese build
 * tampoco admite expresiones en linea, asi que obliga a reescribir las mismas
 * plantillas, y ademas suma una dependencia cuyos fallos vuelven a ser
 * silenciosos. Lo que se usaba de Alpine aqui eran tres interruptores de
 * mostrar/ocultar; no habia framework del que aprovecharse.
 *
 * COMO SE USA
 * -----------
 * Cada control se activa con atributos `data-*`, nunca con JavaScript en
 * linea (que la CSP tambien bloquea). No hay expresiones que interpretar: el
 * comportamiento esta en este archivo y el HTML solo dice "soy esto".
 *
 * Todo es mejora progresiva: sin JavaScript la contrasena sigue siendo un
 * campo de contrasena usable y los paneles quedan abiertos y legibles.
 */

(function () {
  "use strict";

  /** Corre `fn` por cada elemento que case, sin reventar si uno falla. */
  function porCada(selector, raiz, fn) {
    var nodos = (raiz || document).querySelectorAll(selector);
    for (var i = 0; i < nodos.length; i += 1) {
      try {
        fn(nodos[i]);
      } catch (e) {
        // Un control roto no puede tumbar los demas. En una pantalla de
        // cobro, que falle el acordeon no puede impedir cobrar.
        if (window.console) {
          console.error("[ui] fallo al inicializar", selector, e);
        }
      }
    }
  }

  /** Marca el nodo para no inicializarlo dos veces (htmx reinyecta HTML). */
  function yaIniciado(el, marca) {
    if (el.dataset[marca] === "1") return true;
    el.dataset[marca] = "1";
    return false;
  }

  /**
   * Oculta o muestra un elemento via el ATRIBUTO hidden.
   *
   * No es lo mismo que `el.hidden = true`, y la diferencia importa: la
   * propiedad `.hidden` esta definida en HTMLElement, y un `<svg>` es un
   * SVGSVGElement, que hereda de Element y NO de HTMLElement. Asignarle
   * `.hidden` crea una propiedad nueva en el objeto y no oculta nada.
   *
   * Comprobado en navegador: `'hidden' in document.querySelector('svg')` da
   * false, y los iconos no se alternaban aunque el resto del control si
   * funcionaba. El atributo, en cambio, sirve en cualquier elemento porque lo
   * aplica la hoja de estilos del navegador.
   */
  function ocultar(el, oculto) {
    if (!el) return;
    if (oculto) el.setAttribute("hidden", "");
    else el.removeAttribute("hidden");
  }

  // -----------------------------------------------------------------------
  // Mostrar / ocultar contrasena
  // -----------------------------------------------------------------------
  // <div data-password-toggle>
  //   <input type="password" data-password-input>
  //   <button type="button" data-password-button
  //           data-label-mostrar="Mostrar contrasena"
  //           data-label-ocultar="Ocultar contrasena">
  //     <svg data-icono="mostrar">...</svg>
  //     <svg data-icono="ocultar" hidden>...</svg>
  //   </button>
  // </div>
  //
  // El icono "ocultar" nace con el atributo `hidden` en el HTML. Asi no hay
  // destello: el estado inicial correcto lo pinta el servidor, no el
  // JavaScript. Es lo que x-cloak intentaba resolver, resuelto antes.
  function iniciarPassword(contenedor) {
    if (yaIniciado(contenedor, "uiPassword")) return;

    var campo = contenedor.querySelector("[data-password-input]");
    var boton = contenedor.querySelector("[data-password-button]");
    if (!campo || !boton) return;

    var iconoMostrar = contenedor.querySelector('[data-icono="mostrar"]');
    var iconoOcultar = contenedor.querySelector('[data-icono="ocultar"]');

    function pintar(visible) {
      campo.type = visible ? "text" : "password";
      boton.setAttribute(
        "aria-label",
        visible
          ? boton.dataset.labelOcultar || "Ocultar contrasena"
          : boton.dataset.labelMostrar || "Mostrar contrasena"
      );
      boton.setAttribute("aria-pressed", visible ? "true" : "false");
      ocultar(iconoMostrar, visible);
      ocultar(iconoOcultar, !visible);
    }

    boton.addEventListener("click", function () {
      pintar(campo.type === "password");
    });

    pintar(false);
  }

  // -----------------------------------------------------------------------
  // Panel plegable
  // -----------------------------------------------------------------------
  // <div data-collapsible>
  //   <button type="button" data-collapsible-toggle aria-expanded="false">
  //     <svg data-collapsible-chevron>...</svg>
  //   </button>
  //   <div data-collapsible-panel hidden>...</div>
  // </div>
  //
  // `aria-expanded` en el boton y `hidden` en el panel son la fuente de
  // verdad. No hay estado en JavaScript que pueda desincronizarse de lo que
  // el lector de pantalla anuncia.
  function iniciarPlegable(contenedor) {
    if (yaIniciado(contenedor, "uiCollapsible")) return;

    var boton = contenedor.querySelector("[data-collapsible-toggle]");
    var panel = contenedor.querySelector("[data-collapsible-panel]");
    if (!boton || !panel) return;

    var chevron = contenedor.querySelector("[data-collapsible-chevron]");

    if (!panel.id) {
      panel.id = "plegable-" + Math.random().toString(36).slice(2, 10);
    }
    boton.setAttribute("aria-controls", panel.id);

    function pintar(abierto) {
      boton.setAttribute("aria-expanded", abierto ? "true" : "false");
      ocultar(panel, !abierto);
      if (chevron) chevron.classList.toggle("rotate-180", abierto);
    }

    boton.addEventListener("click", function () {
      pintar(boton.getAttribute("aria-expanded") !== "true");
    });

    pintar(boton.getAttribute("aria-expanded") === "true");
  }

  // -----------------------------------------------------------------------
  // Aviso que se cierra solo
  // -----------------------------------------------------------------------
  // <div class="toast" role="alert" data-toast data-toast-ms="6000">
  //   <button type="button" data-toast-close>...</button>
  // </div>
  function iniciarToast(toast) {
    if (yaIniciado(toast, "uiToast")) return;

    var cerrar = toast.querySelector("[data-toast-close]");
    if (cerrar) {
      cerrar.addEventListener("click", function () {
        toast.remove();
      });
    }

    // 0 o ausente = no se cierra solo. Un aviso de error que desaparece solo
    // es un aviso que el cajero puede no llegar a leer, asi que la decision
    // se toma en la plantilla, por aviso.
    var ms = parseInt(toast.dataset.toastMs || "0", 10);
    if (ms > 0) {
      window.setTimeout(function () {
        toast.remove();
      }, ms);
    }
  }

  function iniciarTodo(raiz) {
    porCada("[data-password-toggle]", raiz, iniciarPassword);
    porCada("[data-collapsible]", raiz, iniciarPlegable);
    porCada("[data-toast]", raiz, iniciarToast);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      iniciarTodo(document);
    });
  } else {
    iniciarTodo(document);
  }

  // htmx reemplaza trozos de HTML sin recargar la pagina, y el HTML nuevo
  // llega sin inicializar. Sin esto, un control dentro de un fragmento
  // intercambiado por htmx queda muerto -- el mismo sintoma que veniamos a
  // arreglar, por otra via.
  document.addEventListener("htmx:afterSwap", function (evento) {
    iniciarTodo(evento.target || document);
  });

  window.SamyUI = { iniciar: iniciarTodo };
})();
