/**
 * Lector de códigos de SAMY Cloud.
 *
 * Estrategia en dos niveles:
 *
 *   1. `BarcodeDetector`, la API nativa del navegador. Corre en el hilo de
 *      composición del navegador, usa aceleración del sistema y no descarga
 *      nada. En Chrome/Edge de Android y en escritorio es notablemente más
 *      rápida y precisa que cualquier biblioteca en JavaScript.
 *
 *   2. ZXing como respaldo, cargado **solo si hace falta** (Safari de iOS aún
 *      no expone BarcodeDetector). Cargarlo siempre penalizaría con ~200 KB a
 *      la mayoría de los usuarios que no lo necesitan.
 *
 * Decisiones que importan en un punto de venta:
 *
 * - La cámara se abre SOLO tras un gesto explícito del usuario. Los
 *   navegadores lo exigen, y además pedir permiso al cargar la página es la
 *   forma más rápida de que alguien lo deniegue para siempre.
 * - La cámara se apaga en cuanto hay una lectura válida, al ocultar la
 *   pestaña y al desmontar. Una cámara encendida de más consume batería y es
 *   un problema de privacidad en un mostrador.
 * - Se exige leer el mismo código dos veces seguidas antes de aceptarlo. Con
 *   un solo acierto, un reflejo o un código vecino producen lecturas falsas,
 *   y aquí una lectura falsa significa pagar el recibo de otra persona.
 * - Siempre hay captura manual. Un recibo arrugado o mal impreso no se lee, y
 *   el cajero no puede quedarse bloqueado.
 *
 * Uso:
 *
 *   const scanner = new SamyScanner({
 *     video: document.querySelector('#preview'),
 *     formats: ['code_128', 'ean_13', 'qr_code'],
 *     onDetect: (value, format) => { ... },
 *     onError:  (error) => { ... },
 *   });
 *   await scanner.start();
 */

'use strict';

/** Formatos que puede traer un recibo de servicios o un producto. */
const DEFAULT_FORMATS = ['code_128', 'ean_13', 'ean_8', 'code_39', 'itf', 'qr_code'];

/** Lecturas idénticas consecutivas necesarias para aceptar un código. */
const CONFIRMATIONS_REQUIRED = 2;

/** Techo de detecciones por segundo. Más no mejora la lectura y calienta el equipo. */
const SCAN_FPS = 10;

/**
 * Respaldo autoalojado. Lo copia `npm run vendor` desde node_modules; la
 * política de seguridad de contenido no permite traerlo de un CDN.
 */
const ZXING_URL = '/static/vendor/zxing.min.js';

class SamyScanner {
  constructor(options) {
    this.video = options.video;
    this.formats = options.formats || DEFAULT_FORMATS;
    this.onDetect = options.onDetect || (() => {});
    this.onError = options.onError || (() => {});
    this.onStatus = options.onStatus || (() => {});

    this.stream = null;
    this.detector = null;
    this.zxingReader = null;
    this.running = false;
    this.rafId = null;
    this.lastScanAt = 0;

    // Confirmación por repetición.
    this.candidate = null;
    this.candidateCount = 0;

    this._onVisibilityChange = this._onVisibilityChange.bind(this);
  }

  // --------------------------------------------------------------------
  // Capacidades
  // --------------------------------------------------------------------

  static hasCameraSupport() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  }

  static hasNativeDetector() {
    return typeof window.BarcodeDetector !== 'undefined';
  }

  /**
   * Un origen no seguro no da acceso a la cámara. Es la causa número uno de
   * "no me funciona el escáner" al probar desde el celular contra la IP local
   * del equipo de desarrollo.
   */
  static isSecureContext() {
    return window.isSecureContext ||
           location.protocol === 'https:' ||
           location.hostname === 'localhost' ||
           location.hostname === '127.0.0.1';
  }

  // --------------------------------------------------------------------
  // Ciclo de vida
  // --------------------------------------------------------------------

  async start() {
    if (this.running) return;

    if (!SamyScanner.isSecureContext()) {
      this.onError({
        code: 'insecure_context',
        message: 'La cámara requiere HTTPS. Abre SAMY Cloud con https:// o usa captura manual.',
      });
      return;
    }

    if (!SamyScanner.hasCameraSupport()) {
      this.onError({
        code: 'no_camera_api',
        message: 'Este navegador no permite usar la cámara. Usa captura manual.',
      });
      return;
    }

    this.onStatus({ state: 'requesting_permission' });

    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        video: {
          // 'environment' = cámara trasera en celular; en laptop cae a la
          // única disponible sin fallar.
          facingMode: { ideal: 'environment' },
          // Resolución suficiente para un código de barras impreso sin pedir
          // 4K, que en gama baja tira los cuadros por segundo al suelo.
          width: { ideal: 1280 },
          height: { ideal: 720 },
          // El enfoque continuo es lo que hace que el código "entre solo".
          focusMode: { ideal: 'continuous' },
        },
        audio: false,
      });
    } catch (error) {
      this.onError(this._describeCameraError(error));
      return;
    }

    this.video.srcObject = this.stream;
    this.video.setAttribute('playsinline', 'true'); // iOS: no abrir a pantalla completa
    this.video.setAttribute('muted', 'true');

    try {
      await this.video.play();
    } catch (error) {
      this.stop();
      this.onError({
        code: 'playback_failed',
        message: 'No se pudo iniciar la vista de la cámara.',
      });
      return;
    }

    await this._initDetector();

    this.running = true;
    this.onStatus({ state: 'scanning' });
    document.addEventListener('visibilitychange', this._onVisibilityChange);
    this._startDetection();
  }

  /**
   * Cada motor se conduce distinto:
   *
   * - `BarcodeDetector` decodifica el cuadro que se le pase, asi que lo
   *   gobierna nuestro propio bucle y podemos limitar los cuadros por segundo.
   * - ZXing trae su bucle interno y avisa por callback. Intentar llamarlo
   *   cuadro a cuadro lo pondria a competir consigo mismo.
   */
  _startDetection() {
    if (this.detector) {
      this._scanLoop();
      return;
    }

    if (this.zxingReader) {
      this.zxingReader
        .decodeFromVideoElementContinuously(this.video, (result) => {
          if (!this.running || !result) return;
          this._handleCandidate(result.getText(), 'zxing');
        })
        .catch(() => {
          this.onError({
            code: 'zxing_failed',
            message: 'El lector no pudo iniciar. Captura la referencia manualmente.',
          });
          this.stop();
        });
    }
  }

  stop() {
    this.running = false;

    if (this.rafId) {
      cancelAnimationFrame(this.rafId);
      this.rafId = null;
    }

    if (this.stream) {
      // Detener cada pista es lo que apaga la luz de la cámara. Basta con
      // limpiar srcObject: el navegador puede mantener el dispositivo activo.
      this.stream.getTracks().forEach((track) => track.stop());
      this.stream = null;
    }

    if (this.video) {
      this.video.srcObject = null;
    }

    if (this.zxingReader && typeof this.zxingReader.reset === 'function') {
      this.zxingReader.reset();
    }

    this.candidate = null;
    this.candidateCount = 0;

    document.removeEventListener('visibilitychange', this._onVisibilityChange);
    this.onStatus({ state: 'stopped' });
  }

  /** Apaga la cámara si el usuario cambia de pestaña o bloquea el teléfono. */
  _onVisibilityChange() {
    if (document.hidden && this.running) {
      this.stop();
      this.onStatus({ state: 'paused_hidden' });
    }
  }

  // --------------------------------------------------------------------
  // Detección
  // --------------------------------------------------------------------

  async _initDetector() {
    if (SamyScanner.hasNativeDetector()) {
      try {
        const supported = await window.BarcodeDetector.getSupportedFormats();
        const usable = this.formats.filter((f) => supported.includes(f));
        if (usable.length > 0) {
          this.detector = new window.BarcodeDetector({ formats: usable });
          this.onStatus({ state: 'detector_ready', engine: 'native' });
          return;
        }
      } catch (error) {
        // Cae al respaldo sin ruido: no es un fallo para el usuario.
      }
    }

    await this._initZxingFallback();
  }

  /**
   * Carga ZXing solo cuando hace falta.
   *
   * Se inyecta como <script> y no con `import()` porque el paquete publica un
   * bundle UMD, no un módulo ES: importarlo como módulo falla en tiempo de
   * ejecución. El archivo se sirve desde nuestro propio origen (npm run
   * vendor) porque la política de seguridad de contenido no permite CDN.
   */
  async _initZxingFallback() {
    try {
      await SamyScanner._loadScriptOnce(ZXING_URL, 'ZXing');
      const { BrowserMultiFormatReader } = window.ZXing;
      this.zxingReader = new BrowserMultiFormatReader();
      this.onStatus({ state: 'detector_ready', engine: 'zxing' });
    } catch (error) {
      this.onError({
        code: 'no_detector',
        message: 'No se pudo cargar el lector de códigos. Usa captura manual.',
      });
    }
  }

  /** Inyecta un script una sola vez y espera a que defina su variable global. */
  static _loadScriptOnce(url, globalName) {
    if (window[globalName]) return Promise.resolve();
    if (SamyScanner._pendingScripts[url]) return SamyScanner._pendingScripts[url];

    SamyScanner._pendingScripts[url] = new Promise((resolve, reject) => {
      const tag = document.createElement('script');
      tag.src = url;
      tag.async = true;
      tag.onload = () =>
        window[globalName]
          ? resolve()
          : reject(new Error(`${url} no definió ${globalName}`));
      tag.onerror = () => reject(new Error(`no se pudo descargar ${url}`));
      document.head.appendChild(tag);
    });

    return SamyScanner._pendingScripts[url];
  }

  _scanLoop() {
    if (!this.running) return;

    const now = performance.now();
    const interval = 1000 / SCAN_FPS;

    if (now - this.lastScanAt >= interval) {
      this.lastScanAt = now;
      this._detectFrame();
    }

    this.rafId = requestAnimationFrame(() => this._scanLoop());
  }

  async _detectFrame() {
    if (!this.detector || !this.video || this.video.readyState < 2) return;

    let codes = null;
    try {
      codes = await this.detector.detect(this.video);
    } catch (error) {
      // Un cuadro que no se pudo decodificar es lo normal, no un error.
      return;
    }

    if (!codes || codes.length === 0) return;
    this._handleCandidate(codes[0].rawValue, codes[0].format);
  }

  /**
   * Confirmación por repetición, compartida por los dos motores.
   *
   * Se exige leer el mismo código dos veces seguidas: con un solo acierto, un
   * reflejo o el código de al lado producen lecturas falsas, y aquí una
   * lectura falsa significa pagar el recibo de otra persona.
   */
  _handleCandidate(value, format) {
    if (!this.running || !value) return;

    const cleaned = String(value).trim();
    if (!cleaned) return;

    // Confirmación por repetición: un solo acierto no basta.
    if (cleaned === this.candidate) {
      this.candidateCount += 1;
    } else {
      this.candidate = cleaned;
      this.candidateCount = 1;
    }

    if (this.candidateCount < CONFIRMATIONS_REQUIRED) {
      this.onStatus({ state: 'confirming', progress: this.candidateCount });
      return;
    }

    // Lectura aceptada: se apaga la cámara ANTES de avisar, para que la
    // pantalla siguiente no aparezca con la cámara aún encendida.
    this.stop();
    this._vibrate();
    this.onDetect(cleaned, format);
  }

  /** Confirmación háptica: en un mostrador ruidoso es más fiable que un sonido. */
  _vibrate() {
    if (navigator.vibrate) {
      try { navigator.vibrate(60); } catch (e) { /* ignorado */ }
    }
  }

  _describeCameraError(error) {
    const name = error && error.name ? error.name : '';
    switch (name) {
      case 'NotAllowedError':
      case 'SecurityError':
        return {
          code: 'permission_denied',
          message:
            'No diste permiso para usar la cámara. Puedes habilitarlo en los ' +
            'ajustes del navegador o capturar la referencia manualmente.',
        };
      case 'NotFoundError':
      case 'DevicesNotFoundError':
        return {
          code: 'no_camera',
          message: 'No se encontró ninguna cámara en este dispositivo.',
        };
      case 'NotReadableError':
      case 'TrackStartError':
        return {
          code: 'camera_busy',
          message: 'Otra aplicación está usando la cámara. Ciérrala e intenta de nuevo.',
        };
      case 'OverconstrainedError':
        return {
          code: 'constraints',
          message: 'La cámara no soporta la configuración solicitada.',
        };
      default:
        return {
          code: 'camera_error',
          message: 'No se pudo abrir la cámara. Usa captura manual.',
        };
    }
  }
}

// ---------------------------------------------------------------------------
// Componente Alpine.js
// ---------------------------------------------------------------------------

/**
 * Envoltorio para usarlo declarativamente desde una plantilla.
 *
 * La validación del código leído NO se hace aquí: se envía al servidor. Un
 * validador en el navegador es una ayuda de usabilidad, nunca una garantía —
 * cualquiera puede saltárselo, y con dinero de por medio la única validación
 * que cuenta es la del servidor.
 */
function registrarComponenteAlpine() {
  window.Alpine.data('samyScanner', (config = {}) => ({
    scanner: null,
    active: false,
    status: 'idle',
    errorMessage: '',
    manualValue: '',
    detectedValue: '',
    confirmProgress: 0,
    /** Formato del ultimo codigo leido ('code_128', 'ean_13', 'manual'...). */
    lastFormat: '',
    /** Motor que decodifico: 'native' | 'zxing' | '' si aun no se sabe. */
    engine: '',

    init() {
      // Apagar la cámara al salir de la pantalla, incluso navegando con HTMX.
      this.$watch('active', (value) => {
        if (!value && this.scanner) this.scanner.stop();
      });
      window.addEventListener('beforeunload', () => {
        if (this.scanner) this.scanner.stop();
      });
    },

    async startScan() {
      this.errorMessage = '';
      this.detectedValue = '';
      this.confirmProgress = 0;
      this.active = true;
      this.status = 'starting';

      this.scanner = new SamyScanner({
        video: this.$refs.video,
        formats: config.formats,
        onDetect: (value, format) => {
          this.detectedValue = value;
          this.lastFormat = format;
          this.active = false;
          this.status = 'detected';
          this.$dispatch('code-detected', { value, format });
        },
        onError: (error) => {
          this.errorMessage = error.message;
          this.active = false;
          this.status = 'error';
        },
        onStatus: (info) => {
          this.status = info.state;
          if (info.state === 'confirming') {
            this.confirmProgress = info.progress;
          }
          if (info.state === 'detector_ready') {
            this.engine = info.engine;
          }
        },
      });

      await this.scanner.start();
    },

    stopScan() {
      if (this.scanner) this.scanner.stop();
      this.active = false;
      this.status = 'idle';
    },

    submitManual() {
      const value = this.manualValue.trim();
      if (!value) return;
      this.detectedValue = value;
      this.lastFormat = 'manual';
      this.status = 'detected';
      this.$dispatch('code-detected', { value, format: 'manual' });
    },

    get canScan() {
      return SamyScanner.hasCameraSupport() && SamyScanner.isSecureContext();
    },

    /**
     * Capacidades reales del equipo. La pantalla de diagnostico las muestra
     * tal cual: cuando un cajero reporta "no me lee", esto separa en un
     * vistazo un problema de camara de un recibo mal impreso.
     */
    get caps() {
      return {
        camera: SamyScanner.hasCameraSupport(),
        secure: SamyScanner.isSecureContext(),
        native: SamyScanner.hasNativeDetector(),
      };
    },

    get engineLabel() {
      if (this.engine === 'native') return 'nativo del navegador';
      if (this.engine === 'zxing') return 'ZXing (respaldo)';
      return this.lastFormat === 'manual' ? 'captura manual' : '-';
    },
  }));
}

// El orden importa: Alpine dispara "alpine:init" y arranca en cuanto se
// ejecuta su script. Si este archivo llega antes (el caso normal, porque
// base.html carga Alpine al final), se espera al evento. Si por lo que sea
// Alpine ya estaba cargado, se registra en el acto para no perder el tren.
if (window.Alpine) {
  registrarComponenteAlpine();
} else {
  document.addEventListener('alpine:init', registrarComponenteAlpine);
}

/** Descargas de scripts en curso, para no pedir el mismo archivo dos veces. */
SamyScanner._pendingScripts = {};

window.SamyScanner = SamyScanner;
