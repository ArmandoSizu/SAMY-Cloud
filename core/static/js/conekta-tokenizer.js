/*
 * Tokenizador Web de Conekta.
 *
 * Por que este archivo existe y no es un <script> en la plantilla
 * ---------------------------------------------------------------
 * La CSP de produccion es "script-src 'self' https://pay.conekta.com", sin
 * 'unsafe-inline' y sin nonce. Un <script> escrito dentro de card.html se
 * ejecuta sin problemas en desarrollo (donde no hay CSP) y el navegador lo
 * BLOQUEA en produccion. El sintoma seria el peor posible: la pantalla de
 * cobro carga entera, con su total y sus avisos, y el formulario de la
 * tarjeta simplemente no aparece. Sirviendolo como archivo propio, el mismo
 * codigo pasa la CSP.
 *
 * Por que ya no usa Alpine
 * ------------------------
 * La misma CSP que obliga a lo anterior tampoco lleva 'unsafe-eval', y Alpine
 * evalua sus expresiones (x-show, x-text) construyendo funciones a partir de
 * cadenas. Bajo la politica real cada directiva lanza
 *
 *     EvalError: Evaluating a string as JavaScript violates the following
 *     Content Security Policy directive: script-src 'self' ...
 *
 * Comprobado en navegador el 13/09/2026 con CSP_ENFORCE=True. En esta
 * pantalla el efecto era grave y silencioso a la vez: la pantalla se pinta
 * completa, pero "Cobrando...", el mensaje de error y el aviso de respuesta
 * indeterminada NO se mostraban nunca, porque los tres dependian de x-show.
 * Es decir: el cajero podia cobrar y no ver ni confirmacion ni advertencia.
 *
 * Ahora el estado se pinta poniendo y quitando el atributo `hidden`. No hay
 * nada que evaluar, asi que no hay nada que la CSP pueda bloquear.
 *
 * La llave publica no se interpola aqui: llega por data-attribute desde la
 * plantilla. Asi este archivo es estatico y cacheable, y no hay JavaScript
 * generado por servidor con valores dentro.
 *
 * Que NO hace este archivo
 * ------------------------
 * No toca, no lee y no registra el numero de tarjeta ni el CVV. Esos campos
 * viven dentro de un iframe de pay.conekta.com, en otro origen: el navegador
 * impide leerlos desde aqui aunque alguien lo intentara. Lo unico que cruza
 * es un identificador de token de un solo uso, y de el solo se registran los
 * ultimos cuatro caracteres cuando el modo de depuracion esta activo.
 */

(function () {
  'use strict';

  var CONTENEDOR = '#conektaIframeContainer';

  /* Limite de espera del cobro, en milisegundos.
   *
   * Tiene que ser MAYOR que la suma de los limites del servidor, para que el
   * navegador no se rinda mientras el servidor sigue trabajando y acabe
   * contando una historia distinta a la que quedo registrada:
   *
   *   core -> payments  : 30 s de lectura (ver ServiceClient en pay_card)
   *   payments -> Conekta: 20 s de lectura
   *
   * 45 s deja margen para el peor caso y sigue estando muy por debajo de lo
   * que una persona en un mostrador aguantaria mirando una pantalla. */
  var TIEMPO_LIMITE_MS = 45000;

  /* Oculta o muestra via el ATRIBUTO hidden, no via la propiedad.
   *
   * Se repite aqui en vez de importarlo de ui.js a proposito: esta es la
   * pantalla que cobra, y no puede quedar a merced del orden de carga de otro
   * archivo. Cuatro lineas duplicadas cuestan menos que un cobro sin
   * confirmacion en pantalla.
   *
   * La propiedad `.hidden` solo existe en HTMLElement; un <svg> es
   * SVGSVGElement y no la tiene. El atributo sirve en cualquier elemento. */
  function ocultar(el, oculto) {
    if (!el) return;
    if (oculto) el.setAttribute('hidden', '');
    else el.removeAttribute('hidden');
  }

  /* Registro con datos sensibles fuera. Solo habla si la plantilla lo pide
     con data-debug, para no dejar ruido en la consola de una caja real. */
  function crearRegistro(activo) {
    return function (evento, detalle) {
      if (!activo || !window.console) return;
      console.log('[conekta] ' + evento, detalle === undefined ? '' : detalle);
    };
  }

  /* De un token solo se muestran los ultimos 4 caracteres: suficiente para
     cotejarlo contra el panel de Conekta, inutil para reutilizarlo. */
  function tokenParcial(id) {
    if (typeof id !== 'string' || id.length < 8) return '(no disponible)';
    return '...' + id.slice(-4);
  }

  /* Los cuatro estados de la pantalla, y son excluyentes.
   *
   * Se pintan desde un solo sitio para que no puedan contradecirse. Antes eran
   * tres banderas reactivas independientes (procesando / error /
   * indeterminado) y nada impedia que dos fueran verdad a la vez: se podia
   * llegar a mostrar "Cobrando..." y un error al mismo tiempo. */
  var FORMULARIO = 'FORMULARIO';     // el iframe de Conekta, esperando datos
  var PROCESANDO = 'PROCESANDO';     // cobro enviado, sin respuesta aun
  var INDETERMINADO = 'INDETERMINADO'; // no se sabe como termino
  var FALLO = 'FALLO';               // la tarjeta se rechazo; se puede reintentar

  function crearPantalla(raiz) {
    var partes = {
      area: raiz.querySelector('[data-card-form-area]'),
      error: raiz.querySelector('[data-card-error]'),
      procesando: raiz.querySelector('[data-card-processing]'),
      indeterminado: raiz.querySelector('[data-card-indeterminate]'),
      formulario: raiz.querySelector('[data-card-post-form]'),
      token: raiz.querySelector('[data-card-token]'),
      contenedor: raiz.querySelector(CONTENEDOR),
    };

    /* Pinta UN estado. El mensaje solo se usa en FALLO.
     *
     * Al ocultar el area del formulario NO se desmonta el iframe de Conekta:
     * volver a montarlo costaria otra peticion a su servidor y perderia lo
     * que el cliente hubiera escrito. Solo se esconde. */
    function pintar(estado, mensaje) {
      ocultar(partes.area, estado !== FORMULARIO && estado !== FALLO);
      ocultar(partes.procesando, estado !== PROCESANDO);
      ocultar(partes.indeterminado, estado !== INDETERMINADO);

      var hayError = estado === FALLO && !!mensaje;
      ocultar(partes.error, !hayError);
      if (partes.error) {
        // textContent, nunca innerHTML: el mensaje viene del proveedor y
        // meterlo como HTML seria una inyeccion con su bendicion.
        partes.error.textContent = hayError ? mensaje : '';
      }
    }

    return { partes: partes, pintar: pintar };
  }

  /* Envia el token y RESUELVE la pantalla pase lo que pase.
   *
   * Antes esto era un requestSubmit() sobre un formulario oculto: se confiaba
   * en que el navegador siguiera la redireccion 302 al comprobante. Cuando esa
   * navegacion no ocurre -por lo que sea- la pantalla se queda en
   * "Cobrando..." para siempre, mientras el cobro ya se hizo. Le paso a la
   * primera prueba real con tarjeta.
   *
   * Con fetch la respuesta se recibe en JavaScript y la navegacion la decide
   * esta funcion, que ademas tiene un limite de tiempo. El cajero nunca se
   * queda mirando un giro infinito.
   *
   * NO se reintenta nunca de forma automatica. Si la peticion expira, el cobro
   * puede haberse realizado igualmente: repetirlo seria arriesgar un doble
   * cargo. Se manda al comprobante, que consulta el estado REAL. */
  function enviarCobro(pantalla, raiz, tokenId, log) {
    var partes = pantalla.partes;
    var comprobante = raiz.dataset.receiptUrl;

    pantalla.pintar(PROCESANDO);
    partes.token.value = tokenId;

    var cuerpo = new FormData(partes.formulario);
    var controlador = new AbortController();
    var corte = setTimeout(function () {
      controlador.abort();
    }, TIEMPO_LIMITE_MS);

    fetch(partes.formulario.action, {
      method: 'POST',
      body: cuerpo,
      credentials: 'same-origin',
      redirect: 'follow',
      headers: { 'X-Requested-With': 'fetch' },
      signal: controlador.signal,
    })
      .then(function (respuesta) {
        clearTimeout(corte);
        log('cobro_respondido', {
          estado: respuesta.status,
          redirigido: respuesta.redirected,
        });
        // El servidor redirige al comprobante; fetch ya lo siguio, asi que
        // respuesta.url es la pantalla final.
        window.location.assign(respuesta.url || comprobante);
      })
      .catch(function (excepcion) {
        clearTimeout(corte);
        pantalla.pintar(INDETERMINADO);
        log('cobro_sin_respuesta', { motivo: excepcion.name });
      });
  }

  function iniciar(raiz) {
    var pantalla = crearPantalla(raiz);
    var partes = pantalla.partes;
    var log = crearRegistro(raiz.dataset.debug === '1');

    /* Estos tres son estructura de la plantilla, no datos del proveedor. Si
       falta alguno, el cobro no puede funcionar y hay que decirlo aqui y
       ahora en vez de fallar a mitad de una venta. */
    if (!partes.contenedor || !partes.formulario || !partes.token) {
      pantalla.pintar(FALLO, 'No se encontro el area del formulario de pago.');
      log('estructura_incompleta', {
        contenedor: !!partes.contenedor,
        formulario: !!partes.formulario,
        token: !!partes.token,
      });
      return;
    }

    if (!window.ConektaCheckoutComponents ||
        typeof window.ConektaCheckoutComponents.Card !== 'function') {
      pantalla.pintar(
        FALLO,
        'No se pudo cargar el formulario de pago de Conekta. ' +
          'Revisa la conexion y vuelve a intentarlo.'
      );
      log('script_no_cargado');
      return;
    }

    var llave = raiz.dataset.publicKey || '';
    if (!llave) {
      pantalla.pintar(FALLO, 'Falta la configuracion del proveedor de pago.');
      log('sin_llave_publica');
      return;
    }

    pantalla.pintar(FORMULARIO);

    log('inicializando', {
      contenedor: CONTENEDOR,
      alto: partes.contenedor.getBoundingClientRect().height,
      locale: 'es',
    });

    try {
      window.ConektaCheckoutComponents.Card({
        config: {
          targetIFrame: CONTENEDOR,
          publicKey: llave,
          locale: 'es',
        },
        callbacks: {
          /* Se dispara cuando el iframe termina de montarse. Es la senal de
             que el formulario esta realmente en pantalla. */
          onGetInfoSuccess: function (info) {
            raiz.dataset.cardReady = '1';
            log('formulario_montado', {
              alto: partes.contenedor.getBoundingClientRect().height,
              iframes: partes.contenedor.querySelectorAll('iframe').length,
              // Se registra la FORMA de la respuesta, nunca su contenido.
              claves: info && typeof info === 'object' ? Object.keys(info) : [],
            });
          },

          onCreateTokenSucceeded: function (token) {
            /* Se envia en cuanto llega: el token es de un solo uso y caduca a
               los diez minutos, asi que no hay nada que ganar esperando. */
            log('token_creado', { token: tokenParcial(token && token.id) });
            enviarCobro(pantalla, raiz, (token && token.id) || '', log);
          },

          onCreateTokenError: function (error) {
            /* Se muestra el mensaje del proveedor tal cual. Traducirlo a un
               "algo salio mal" generico deja al cajero sin saber si el
               problema es la tarjeta, la fecha o el CVV. */
            var mensaje =
              (error && (error.message_to_purchaser || error.message)) ||
              'No se pudo procesar la tarjeta. Verifica los datos.';
            pantalla.pintar(FALLO, mensaje);
            log('token_rechazado', {
              // El mensaje del proveedor describe el motivo, no la tarjeta.
              // Nunca se registra el PAN ni el CVV.
              motivo: mensaje,
              codigo: (error && (error.code || error.type)) || '',
            });
          },
        },
      });
    } catch (excepcion) {
      pantalla.pintar(
        FALLO,
        'No se pudo iniciar el formulario de pago: ' + excepcion.message
      );
      log('excepcion_al_inicializar', excepcion.message);
    }
  }

  function arrancar() {
    var raiz = document.querySelector('[data-card-checkout]');
    if (raiz) iniciar(raiz);
  }

  /* Este archivo se carga en extra_scripts, al final del <body>, asi que el
     DOM ya esta listo. Se cubre igualmente el caso contrario: si algun dia
     alguien lo mueve al <head>, querySelector devolveria null y la pantalla
     de cobro se quedaria vacia sin dar ningun error. */
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', arrancar);
  } else {
    arrancar();
  }
})();
