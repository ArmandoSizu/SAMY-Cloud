"""Adaptador de Linntae (proveedor mexicano de tiempo aire y servicios).

ESTADO: **DEMO. No productivo.** Ver ``docs/providers/linntae.md``.

Implementa ``TopupProvider``. El microservicio no sabe que existe Linntae:
sabe que existe ese contrato.

LAS TRES LLAVES QUE ABREN ESTE ADAPTADOR
----------------------------------------

Tres condiciones independientes, y falta una y no se envia nada:

1. ``LINNTAE_ENV`` coherente con ``LINNTAE_BASE_URL``. ``demo`` solo puede
   hablar con ``apidemo.linn.mx``; ``production`` solo con ``api.linn.mx``.
   Se comprueba en el cliente y no se puede saltar.
2. ``LINNTAE_TYPE_BALANCE`` definido explicitamente. Linntae exige
   ``typeBalance`` en cada compra pero **no publica su enumeracion**. Que
   ``1`` sea la bolsa de plataforma es una inferencia de sus ejemplos, no un
   dato, y mandar el numero equivocado gasta la bolsa equivocada.
3. ``ALLOW_REAL_PROVIDER_TRANSACTIONS=true``. El interruptor final, separado
   del ambiente a proposito: tener el sandbox bien configurado no es permiso
   para operar.

Las tres son de configuracion, no de codigo. No hay que tocar este archivo
para pasar de DEMO a produccion, y eso es deliberado: un cambio de ambiente
que exige editar codigo es un cambio de ambiente que alguien hara mal con
prisa.

LO QUE ESTE ADAPTADOR NO PUEDE HACER, Y LO DICE
-----------------------------------------------

``get_topup_status(folio)`` y ``find_by_custom_identifier(clave)`` son los dos
ganchos genericos de conciliacion. **Con Linntae ninguno de los dos
funciona**, y no por falta de implementacion: su API no ofrece ninguna
consulta que reciba la autorizacion de la compra ni una clave nuestra. Sus
dos consultas identifican la operacion por ``idOffer`` + ``phoneNumber``.

Asi que los dos devuelven "no lo se" -nunca FAILED- y la conciliacion real
entra por ``estado_por_contexto()``, que si recibe los datos con los que
Linntae sabe buscar. Devolver FAILED desde un gancho que no puede preguntar
seria repetir un error que ya costo dinero en este proyecto: pasarle a una
consulta una clave que no es la que espera y leer su "no existe" como "la
recarga no se aplico".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Final

import structlog

from apps.providers.base import (
    CatalogProduct,
    ContextoConciliacion,
    TopupProvider,
    TopupRequest,
    TopupResult,
    TopupStatus,
)
from apps.providers.linntae import conciliacion
from apps.providers.linntae.client import LinntaeClient, LinntaeConfig
from apps.providers.linntae.codigos import Consecuencia, Endpoint
from apps.providers.linntae.parseo import (
    MONEDA,
    CatalogoLinntae,
    ComisionLinntae,
    RespuestaIlegible,
    SaldosLinntae,
    leer_catalogo,
    leer_comisiones,
    leer_saldos,
)
from apps.providers.registry import topup_registry
from samy_common.money import Money
from samy_common.providers.base import (
    ProviderCapability,
    ProviderHealth,
    ProviderStatus,
)
from samy_common.providers.exceptions import (
    ProviderError,
    ProviderIndeterminateError,
    ProviderNotConfigured,
    ProviderPermanentError,
    ProviderTransientError,
)
from samy_common.saldo import SaldoProveedor

__all__ = ["LinntaeProvider", "EsquemaLinntae"]

log = structlog.get_logger("provider.linntae")

#: ``extraComision`` admitidos por la especificacion para tiempo aire.
EXTRA_COMISION_VALIDA: Final[frozenset[int]] = frozenset({0, 1, 2, 3, 4, 5})

#: Largo exacto que Linntae exige para el telefono.
DIGITOS_TELEFONO: Final[int] = 10


@dataclass(frozen=True, slots=True)
class EsquemaLinntae:
    """Esquema comercial que Linntae tiene asignado a nuestra cuenta.

    Lo devuelve ``/config/getScheme`` y es la pista mas directa sobre COMO se
    aplica la comision: el ejemplo de su especificacion trae
    ``"1.-COMISION SOBRE VENTA (tiempo aire y pago de servicios)"``.

    Se lee y se reporta; **no se interpreta para calcular margen**. Que el
    esquema se llame "comision sobre venta" no dice si esa comision abarata
    la recarga o se abona a la bolsa de comision aparte, y esas dos lecturas
    dan costos distintos. Eso se resuelve midiendo el saldo antes y despues de
    una operacion, no leyendo un nombre.
    """

    id: int | None
    nombre: str


@topup_registry.register
class LinntaeProvider(TopupProvider):
    """Linntae: tiempo aire mexicano por API REST. Candidato principal en DEMO."""

    slug = "linntae"
    display_name = "Linntae"
    capabilities = frozenset(
        {ProviderCapability.AIRTIME_TOPUP, ProviderCapability.CATALOG_SYNC}
    )
    required_settings = (
        "LINNTAE_BASE_URL",
        "LINNTAE_USERNAME",
        "LINNTAE_PASSWORD",
        "LINNTAE_TYPE_BALANCE",
    )
    requires_commercial_contract = False
    documentation_url = "https://linntae.com"

    def __init__(self, config: LinntaeConfig, mode: Any) -> None:
        super().__init__(config, mode)
        self._client: LinntaeClient | None = None

    # -- cliente -----------------------------------------------------------

    @property
    def client(self) -> LinntaeClient:
        if self._client is None:
            self._client = LinntaeClient(self.config)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- salud -------------------------------------------------------------

    def check_health(self) -> ProviderHealth:
        """Comprueba de verdad: autentica y consulta saldo contra Linntae.

        No hay rama que devuelva READY sin haber hablado con ellos. Y el
        ``typeBalance`` entra aqui, no solo en la compra: si faltara y esto
        dijera READY, el catalogo quedaria vendible y la secuencia seria
        cobrar al cliente y descubrir despues que no se puede mandar la
        recarga. Ese es justo el fallo que el proyecto prohibe.
        """
        problemas = self.config.problemas_de_ambiente()
        if problemas:
            return ProviderHealth(
                status=ProviderStatus.NOT_CONFIGURED,
                detail=" ".join(problemas),
                missing_requirements=problemas,
            )

        faltantes = [
            nombre
            for nombre, valor in (
                ("LINNTAE_BASE_URL", self.config.base_url),
                ("LINNTAE_USERNAME", self.config.username),
                ("LINNTAE_PASSWORD", self.config.password),
            )
            if not valor
        ]
        if faltantes:
            return ProviderHealth(
                status=ProviderStatus.NOT_CONFIGURED,
                detail=(
                    "Faltan credenciales de Linntae: "
                    f"{', '.join(faltantes)}. Se ponen en el .env, nunca en el "
                    "repositorio."
                ),
                missing_requirements=tuple(faltantes),
            )

        inicio = time.perf_counter()
        try:
            saldos = self._saldos()
        except ProviderNotConfigured as exc:
            return ProviderHealth(
                status=ProviderStatus.NOT_CONFIGURED,
                detail=exc.message,
                missing_requirements=exc.missing_requirements,
            )
        except ProviderPermanentError as exc:
            return ProviderHealth(
                status=ProviderStatus.NOT_CONFIGURED,
                detail=f"Linntae rechazo la peticion: {exc.message}",
                missing_requirements=("Credenciales validas de Linntae",),
            )
        except ProviderError as exc:
            return ProviderHealth(
                status=ProviderStatus.DEGRADED,
                detail=f"No se pudo contactar a Linntae: {exc.message}",
            )

        if saldos.plataforma is None:
            return ProviderHealth(
                status=ProviderStatus.DEGRADED,
                detail=(
                    "Linntae respondio pero no se reconocio el saldo de "
                    "plataforma en su respuesta. No se afirma que haya fondos "
                    f"sobre una respuesta que no se entendio. Crudo: "
                    f"{saldos.crudos or 'sin campos de saldo'}."
                ),
                missing_requirements=("Saldo de plataforma legible",),
            )

        if not saldos.plataforma.is_positive:
            return ProviderHealth(
                status=ProviderStatus.DEGRADED,
                detail=(
                    f"Linntae opera pero el saldo de plataforma es "
                    f"{saldos.plataforma}. Es una cuenta prepagada: sin saldo "
                    "no puede recargar."
                ),
                missing_requirements=("Saldo fondeado en la cuenta de Linntae",),
            )

        if self.config.type_balance is None:
            return ProviderHealth(
                status=ProviderStatus.DEGRADED,
                detail=(
                    "Linntae autentica y tiene saldo, pero falta "
                    "LINNTAE_TYPE_BALANCE. Su API exige 'typeBalance' en cada "
                    "compra y no publica que numero corresponde a cada bolsa. "
                    "Confirmalo con Linntae y ponlo en el .env: mandar el "
                    "numero equivocado gasta la bolsa equivocada. Mientras "
                    "falte, el producto no es vendible, que es lo correcto."
                ),
                missing_requirements=(
                    "LINNTAE_TYPE_BALANCE confirmado con Linntae",
                ),
            )

        faltan_para_operar: list[str] = []
        if not self.config.permitir_operaciones_reales:
            faltan_para_operar.append("ALLOW_REAL_PROVIDER_TRANSACTIONS=true")

        if faltan_para_operar:
            return ProviderHealth(
                status=ProviderStatus.DEGRADED,
                detail=(
                    f"Linntae listo en modo {self.mode} con saldo "
                    f"{saldos.plataforma}, pero las operaciones estan "
                    "deshabilitadas por bandera. Es el estado esperado hasta "
                    "que se autorice la prueba DEMO."
                ),
                missing_requirements=tuple(faltan_para_operar),
                latency_ms=int((time.perf_counter() - inicio) * 1000),
            )

        return ProviderHealth(
            status=ProviderStatus.READY,
            detail=(
                f"Linntae operativo en modo {self.mode} ({self.config.host}). "
                f"Saldo plataforma: {saldos.plataforma}."
            ),
            latency_ms=int((time.perf_counter() - inicio) * 1000),
        )

    # -- saldo -------------------------------------------------------------

    def _saldos(self) -> SaldosLinntae:
        respuesta = self.client.leer(Endpoint.SALDO, metodo="POST")
        if not respuesta.ok:
            raise ProviderTransientError(
                provider=self.slug,
                message=(
                    f"Linntae devolvio code={respuesta.code} al consultar "
                    f"saldo: {respuesta.mensaje or 'sin mensaje'}."
                ),
                external_code=str(respuesta.code),
            )
        return leer_saldos(respuesta.payload)

    def saldos(self) -> SaldosLinntae:
        """Las tres bolsas de Linntae, para diagnostico y administracion."""
        return self._saldos()

    def saldo_disponible(self) -> SaldoProveedor:
        """Saldo de PLATAFORMA, que es el que paga una recarga.

        No se suma la bolsa de comision. Sumarlas daria un numero mayor que lo
        que realmente se puede gastar: la recarga sale de plataforma, y que la
        comision sea liquida es una pregunta que su especificacion no
        contesta.

        Nunca levanta: la guarda de saldo ya sabe tratar "no lo se", y una
        excepcion aqui tumbaria la creacion del cumplimiento con un 500.
        """
        if not self.config.credenciales_completas:
            return SaldoProveedor(
                disponible=None, detalle="Linntae sin credenciales configuradas."
            )
        if self.config.problemas_de_ambiente():
            return SaldoProveedor(
                disponible=None,
                detalle="La pareja LINNTAE_ENV / LINNTAE_BASE_URL no es coherente.",
            )

        try:
            saldos = self._saldos()
        except ProviderError as exc:
            return SaldoProveedor(
                disponible=None, detalle=f"No se pudo consultar a Linntae: {exc.message}"
            )
        except RespuestaIlegible as exc:
            return SaldoProveedor(disponible=None, detalle=str(exc))

        if saldos.plataforma is None:
            return SaldoProveedor(
                disponible=None,
                detalle=(
                    "Linntae respondio pero el saldo de plataforma no se "
                    "reconocio."
                ),
            )

        return SaldoProveedor(
            disponible=saldos.plataforma,
            detalle=f"Saldo de plataforma Linntae ({self.mode}).",
        )

    # -- catalogo ----------------------------------------------------------

    def catalogo_crudo(self) -> CatalogoLinntae:
        """Catalogo tecnico de Linntae, ya normalizado pero sin guardar.

        Lo usa el diagnostico de solo lectura. No llama a ``ensure_ready()``
        porque leer el catalogo no mueve dinero y hay que poder mirarlo antes
        de decidir si se habilita nada.
        """
        respuesta = self.client.leer(Endpoint.SINCRONIZAR_PRODUCTOS)
        if not respuesta.ok:
            raise ProviderTransientError(
                provider=self.slug,
                message=(
                    f"Linntae devolvio code={respuesta.code} al sincronizar "
                    f"productos: {respuesta.mensaje or 'sin mensaje'}."
                ),
                external_code=str(respuesta.code),
            )
        return leer_catalogo(respuesta.payload)

    def companias_tae(self) -> list[Any]:
        """``/products/taeCompanies``: operadores tradicionales."""
        from apps.providers.linntae.parseo import leer_companias

        respuesta = self.client.leer(Endpoint.COMPANIAS_TAE)
        if not respuesta.ok:
            raise ProviderTransientError(
                provider=self.slug,
                message=f"Linntae devolvio code={respuesta.code} en taeCompanies.",
                external_code=str(respuesta.code),
            )
        return leer_companias(respuesta.payload, seccion="TIEMPO AIRE")

    def companias_virtuales(self) -> list[Any]:
        """``/products/taeVirtualCompanies``: OMV y recargas virtuales."""
        from apps.providers.linntae.parseo import leer_companias

        respuesta = self.client.leer(Endpoint.COMPANIAS_VIRTUALES)
        if not respuesta.ok:
            raise ProviderTransientError(
                provider=self.slug,
                message=f"Linntae devolvio code={respuesta.code} en taeVirtualCompanies.",
                external_code=str(respuesta.code),
            )
        return leer_companias(respuesta.payload, seccion="RECARGA VIRTUAL")

    def fetch_catalog(self) -> list[CatalogProduct]:
        """Catalogo TECNICO de Linntae como ``CatalogProduct``.

        Esto NO es lo que ve el cajero. Es la lista de ofertas que Linntae
        sabe ejecutar; el catalogo comercial es otra cosa y se empareja contra
        esta por identidad exacta (ver ``apps.commercial.mapping``).

        Dos decisiones que importan en cada fila:

        * ``amount_in_sku`` queda en ``True`` siempre que la oferta declare
          importe. Con Linntae es literalmente cierto: ``POST /purchase/tae``
          **no tiene parametro de monto**. El importe vive dentro del
          ``idOffer`` y no hay forma de contradecirlo, que es la mejor version
          de este problema.
        * ``raw["family"]`` se rellena porque es de donde el importador saca
          ``provider_family``, y sin familia el emparejamiento por identidad
          exacta no puede distinguir un saldo de un paquete del mismo precio.
        """
        self.ensure_ready()
        catalogo = self.catalogo_crudo()

        productos: list[CatalogProduct] = []
        for compania in catalogo.companias:
            for oferta in compania.ofertas:
                familia = oferta.categoria or compania.seccion
                etiqueta = oferta.descripcion or (
                    f"{compania.nombre} {oferta.monto}" if oferta.monto else compania.nombre
                )
                productos.append(
                    CatalogProduct(
                        provider_slug=self.slug,
                        provider_product_id=str(oferta.id_offer),
                        operator_code=str(compania.id_operator),
                        operator_name=compania.nombre,
                        label=etiqueta[:160],
                        amount=oferta.monto,
                        is_data_package=bool(
                            oferta.categoria and "internet" in oferta.categoria.lower()
                        ),
                        logo_url=compania.logo_url,
                        raw={
                            "family": familia,
                            "seccion": compania.seccion,
                            "categoria": oferta.categoria,
                            "idOperator": compania.id_operator,
                            "idOffer": oferta.id_offer,
                            "description": oferta.descripcion,
                            "linntae": oferta.raw,
                        },
                    )
                )

        log.info(
            "linntae_catalogo_leido",
            companias=len(catalogo.companias),
            ofertas=len(productos),
            con_sku=len(catalogo.con_sku),
            secciones_desconocidas=list(catalogo.secciones_desconocidas),
        )
        return productos

    # -- comisiones --------------------------------------------------------

    def comisiones(self) -> list[ComisionLinntae]:
        """``/config/getProductsCommissions``: las tasas de NUESTRA cuenta.

        Esta es la fuente de verdad de las comisiones, y por eso en este
        repositorio no hay ningun porcentaje de Linntae escrito a mano. Los
        numeros que dieron comercialmente (alrededor de 6% en operadores
        tradicionales, 5% en virtuales) no estan en el codigo: su propia
        especificacion muestra ejemplos con 5.5%, lo que confirma que depende
        de la cuenta.
        """
        respuesta = self.client.leer(Endpoint.COMISIONES)
        if not respuesta.ok:
            raise ProviderTransientError(
                provider=self.slug,
                message=(
                    f"Linntae devolvio code={respuesta.code} al consultar "
                    f"comisiones: {respuesta.mensaje or 'sin mensaje'}."
                ),
                external_code=str(respuesta.code),
            )
        return leer_comisiones(respuesta.payload)

    def esquema(self) -> EsquemaLinntae:
        """``/config/getScheme``: el esquema comercial de la cuenta."""
        respuesta = self.client.leer(Endpoint.ESQUEMA)
        if not respuesta.ok:
            raise ProviderTransientError(
                provider=self.slug,
                message=f"Linntae devolvio code={respuesta.code} en getScheme.",
                external_code=str(respuesta.code),
            )
        crudo_id = respuesta.payload.get("id")
        try:
            identificador = int(crudo_id) if crudo_id is not None else None
        except (TypeError, ValueError):
            identificador = None
        return EsquemaLinntae(
            id=identificador,
            nombre=str(respuesta.payload.get("name") or "").strip(),
        )

    # -- recarga -----------------------------------------------------------

    def send_topup(self, request: TopupRequest) -> TopupResult:
        """Envia la recarga. Un solo intento, y solo con la orden ya pagada.

        Validaciones antes de tocar la red, todas con el mismo criterio: lo
        que Linntae va a rechazar, se rechaza aqui, donde el error es nuestro
        y barato, en vez de gastar una llamada para que nos lo diga.

        ``Idempotency-Key`` **no se envia.** Su especificacion la documenta
        unicamente en ``POST /purchase/pin``. Mandarla en ``/purchase/tae``
        suponiendo que tambien funciona seria construir la seguridad de las
        recargas sobre una cabecera que quiza ignoren. La proteccion contra
        duplicados de SAMY es suya y no depende de Linntae: un cumplimiento
        por orden, maquina de estados y bloqueo de fila.
        """
        self.ensure_ready()

        if self.config.type_balance is None:
            raise ProviderNotConfigured(
                provider=self.slug,
                message=(
                    "Falta LINNTAE_TYPE_BALANCE. Linntae exige 'typeBalance' en "
                    "cada compra y no publica su enumeracion; no se adivina."
                ),
                missing_requirements=("LINNTAE_TYPE_BALANCE",),
            )

        if self.config.extra_comision not in EXTRA_COMISION_VALIDA:
            raise ProviderNotConfigured(
                provider=self.slug,
                message=(
                    f"LINNTAE_EXTRA_COMISION={self.config.extra_comision} no es "
                    "uno de los valores que acepta la especificacion "
                    f"({sorted(EXTRA_COMISION_VALIDA)})."
                ),
                missing_requirements=("LINNTAE_EXTRA_COMISION valido",),
            )

        telefono = "".join(c for c in request.phone_national if c.isdigit())
        if len(telefono) != DIGITOS_TELEFONO:
            raise ProviderPermanentError(
                provider=self.slug,
                message=(
                    f"Linntae exige 10 digitos y el numero tiene {len(telefono)}. "
                    "No se envia: la recarga se rechazaria igual."
                ),
            )

        try:
            id_offer = int(str(request.product_id).strip())
        except (TypeError, ValueError) as exc:
            raise ProviderPermanentError(
                provider=self.slug,
                message=(
                    f"El identificador de producto '{request.product_id}' no es "
                    "un idOffer de Linntae (entero). El mapping esta mal: no se "
                    "envia nada."
                ),
            ) from exc

        cuerpo = {
            "idOffer": id_offer,
            "phoneNumber": telefono,
            "typeBalance": int(self.config.type_balance),
            "extraComision": int(self.config.extra_comision),
        }

        respuesta = self.client.comprar(Endpoint.COMPRA_TAE, cuerpo)
        autorizacion = str(respuesta.payload.get("authorization") or "").strip()

        if respuesta.consecuencia is Consecuencia.EJECUTADA:
            if not autorizacion:
                # Linntae dice exito y no da autorizacion. Sin ella no hay con
                # que reclamar ni con que verificar despues, asi que no se
                # afirma exito: se concilia.
                raise ProviderIndeterminateError(
                    provider=self.slug,
                    message=(
                        "Linntae devolvio code=0 sin 'authorization'. No se "
                        "cierra como exitosa una recarga que no se puede "
                        "verificar ni reclamar."
                    ),
                    external_reference=request.idempotency_key,
                )
            return TopupResult(
                status=TopupStatus.SUCCEEDED,
                provider_reference=autorizacion,
                provider_mode=str(self.mode),
                operator_reference=autorizacion,
                delivered_amount=request.amount,
                raw_response=dict(respuesta.payload),
            )

        if respuesta.consecuencia is Consecuencia.DUPLICADA:
            # code 24. No es un error: Linntae dice que ya hay una venta igual
            # hoy, y esa venta pudo haberse aplicado. Se concilia antes de
            # tocar nada. NO se llama a /sale/unlock: eso es una decision
            # humana y automatizarlo aqui produciria la segunda recarga.
            raise ProviderIndeterminateError(
                provider=self.slug,
                message=(
                    "Linntae reporta venta duplicada (code 24): "
                    f"{respuesta.mensaje or 'sin detalle'}. Puede que la "
                    "anterior si se aplicara. Se consulta su estado; no se "
                    "reintenta ni se desbloquea."
                ),
                external_code="24",
                external_reference=request.idempotency_key,
            )

        if respuesta.consecuencia is Consecuencia.INDETERMINADA:
            raise ProviderIndeterminateError(
                provider=self.slug,
                message=(
                    f"Respuesta no concluyente de Linntae (HTTP "
                    f"{respuesta.http_status}, code={respuesta.code}): "
                    f"{respuesta.mensaje or 'sin mensaje'}. No se sabe si la "
                    "recarga se aplico."
                ),
                external_code=str(respuesta.code or respuesta.http_status),
                external_reference=request.idempotency_key,
            )

        if respuesta.consecuencia is Consecuencia.GEOBLOQUEO:
            raise ProviderPermanentError(
                provider=self.slug,
                message=(
                    "Linntae rechazo la peticion por el pais de origen antes "
                    f"de procesarla ({respuesta.mensaje}). La recarga no se "
                    "ejecuto. Hay que acordar con Linntae paises o IPs "
                    "permitidas."
                ),
                external_code="PROVIDER_GEO_BLOCKED",
            )

        if respuesta.consecuencia in (
            Consecuencia.AUTENTICACION,
            Consecuencia.PERMISOS,
        ):
            # Un 401 o 403 lo resuelve la capa de seguridad del proveedor
            # ANTES de llegar a la logica de compra, asi que la recarga no
            # entro. Se trata como no ejecutada y no como indeterminada, que
            # es lo que permite reembolsar sin dejarlo en revision manual.
            #
            # El riesgo de esa lectura se acota antes de llegar aqui: el
            # cliente renueva el token si le queda menos de un minuto de vida
            # justamente para que un 401 en mitad de una compra sea raro.
            raise ProviderPermanentError(
                provider=self.slug,
                message=(
                    f"Linntae rechazo la autorizacion (HTTP "
                    f"{respuesta.http_status}) antes de procesar la recarga: "
                    f"{respuesta.mensaje or 'sin mensaje'}."
                ),
                external_code=str(respuesta.http_status),
            )

        # NO_EJECUTADA y NO_EJECUTADA_TRANSITORIA: rechazo antes de tocar al
        # operador (datos invalidos, tipo de saldo, saldo insuficiente,
        # mantenimiento). Nada se movio.
        return TopupResult(
            status=TopupStatus.FAILED,
            provider_reference="",
            provider_mode=str(self.mode),
            failure_reason=(
                f"Linntae rechazo la recarga (code={respuesta.code}): "
                f"{respuesta.mensaje or 'sin mensaje'}"
            )[:255],
            raw_response=dict(respuesta.payload),
        )

    # -- conciliacion ------------------------------------------------------

    def get_topup_status(self, provider_reference: str) -> TopupResult:
        """No se puede contestar con Linntae. Devuelve UNKNOWN, nunca FAILED.

        Su API no tiene ninguna consulta que reciba la ``authorization`` de
        una compra. Ver el docstring del modulo: implementar esto "como se
        pueda" -buscando el folio en el historico, por ejemplo- daria
        respuestas equivocadas, porque el folio de Linntae **no es unico**.

        La conciliacion de verdad entra por ``estado_por_contexto()``.
        """
        log.info(
            "linntae_get_topup_status_no_aplicable",
            provider_reference=provider_reference[:16],
        )
        return TopupResult(
            status=TopupStatus.UNKNOWN,
            provider_reference=provider_reference,
            provider_mode=str(self.mode),
            failure_reason=(
                "Linntae no ofrece consulta por autorizacion. La conciliacion "
                "se hace por idOffer + telefono (estado_por_contexto)."
            ),
        )

    def find_by_custom_identifier(self, custom_identifier: str) -> TopupResult | None:
        """Linntae no acepta referencias nuestras. ``None`` = "no lo se"."""
        return None

    def estado_por_contexto(
        self, contexto: ContextoConciliacion
    ) -> TopupResult | None:
        """Conciliacion real: pregunta con los datos que Linntae si acepta.

        ``None`` significa "no lo se" y deja la recarga en revision. Nunca
        concluye por ausencia de registro: ver la regla de oro en
        ``conciliacion.py``.
        """
        if not self.config.credenciales_completas:
            return None
        if self.config.problemas_de_ambiente():
            return None

        ctx = conciliacion.contexto_desde_datos(
            id_offer=contexto.provider_product_id,
            telefono_nacional=contexto.phone_national,
            monto=contexto.amount,
            enviado_en=contexto.enviado_en,
            autorizacion=contexto.provider_reference,
        )
        try:
            return conciliacion.resolver(self.client, ctx, modo=str(self.mode))
        except (ProviderError, RespuestaIlegible) as exc:
            log.warning("linntae_conciliacion_fallo", error=str(exc)[:200])
            return None

    # -- utilidades --------------------------------------------------------

    @staticmethod
    def money(pesos: str) -> Money:
        """Atajo para pruebas y comandos: pesos como texto a ``Money``."""
        return Money.parse(pesos, MONEDA)
