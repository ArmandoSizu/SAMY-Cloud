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

  function registrarComponente() {
    window.Alpine.data('cobroConTarjeta', function () {
      return {
        procesando: false,
        error: '',
        listo: false,

        init: function () {
          var raiz = this.$el;
          var contenedor = raiz.querySelector(CONTENEDOR);
          var log = crearRegistro(raiz.dataset.debug === '1');
          var self = this;

          if (!contenedor) {
            this.error = 'No se encontro el area del formulario de pago.';
            log('contenedor_ausente');
            return;
          }

          if (!window.ConektaCheckoutComponents ||
              typeof window.ConektaCheckoutComponents.Card !== 'function') {
            this.error =
              'No se pudo cargar el formulario de pago de Conekta. ' +
              'Revisa la conexion y vuelve a intentarlo.';
            log('script_no_cargado');
            return;
          }

          var llave = raiz.dataset.publicKey || '';
          if (!llave) {
            this.error = 'Falta la configuracion del proveedor de pago.';
            log('sin_llave_publica');
            return;
          }

          log('inicializando', {
            contenedor: CONTENEDOR,
            alto: contenedor.getBoundingClientRect().height,
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
                /* Se dispara cuando el iframe termina de montarse. Es la
                   senal de que el formulario esta realmente en pantalla. */
                onGetInfoSuccess: function (info) {
                  self.listo = true;
                  log('formulario_montado', {
                    alto: contenedor.getBoundingClientRect().height,
                    iframes: contenedor.querySelectorAll('iframe').length,
                    // Se registra la FORMA de la respuesta, nunca su contenido.
                    claves: info && typeof info === 'object'
                      ? Object.keys(info) : [],
                  });
                },

                onCreateTokenSucceeded: function (token) {
                  /* Se envia en cuanto llega: el token es de un solo uso y
                     caduca a los diez minutos, asi que no hay nada que ganar
                     esperando. */
                  log('token_creado', { token: tokenParcial(token && token.id) });
                  self.procesando = true;
                  self.$refs.token.value = (token && token.id) || '';
                  self.$refs.formulario.requestSubmit();
                },

                onCreateTokenError: function (error) {
                  /* Se muestra el mensaje del proveedor tal cual. Traducirlo a
                     un "algo salio mal" generico deja al cajero sin saber si
                     el problema es la tarjeta, la fecha o el CVV. */
                  self.procesando = false;
                  self.error =
                    (error && (error.message_to_purchaser || error.message)) ||
                    'No se pudo procesar la tarjeta. Verifica los datos.';
                  log('token_rechazado', {
                    // El mensaje del proveedor describe el motivo, no la
                    // tarjeta. Nunca se registra el PAN ni el CVV.
                    motivo: self.error,
                    codigo: (error && (error.code || error.type)) || '',
                  });
                },
              },
            });
          } catch (excepcion) {
            this.error =
              'No se pudo iniciar el formulario de pago: ' + excepcion.message;
            log('excepcion_al_inicializar', excepcion.message);
          }
        },
      };
    });
  }

  /* Alpine se carga al final del <body>, despues de este archivo, asi que lo
     normal es esperar a alpine:init. Pero si algun dia cambia ese orden, un
     addEventListener tardio no se ejecutaria nunca y el formulario no
     aparaceria sin dar ningun error. Se cubren los dos casos. */
  if (window.Alpine) {
    registrarComponente();
  } else {
    document.addEventListener('alpine:init', registrarComponente);
  }
})();
