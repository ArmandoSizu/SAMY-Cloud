"""Catalogo COMERCIAL de SAMY Cloud.

Esta app es deliberadamente independiente de ``apps.catalog``. La diferencia
no es de organizacion, es de responsabilidad:

* ``apps.catalog`` es el catalogo TECNICO: lo que el proveedor dice que vende
  hoy. Se llena solo, desde ``provider.fetch_catalog()``, y refleja
  identificadores, precios y denominaciones ajenas. En sandbox eso produce
  cosas como "$89.85" y "$179.70", que sirven para probar la tuberia y no
  significan nada para un cliente en un mostrador.

* ``apps.commercial`` es el catalogo COMERCIAL: lo que SAMY Cloud decide
  vender, con el nombre con el que el cliente lo pide ("un Amigo Sin Limite de
  $100"). Se llena a mano, contra la oferta oficial del operador, y cada
  producto guarda de donde salio y cuando se verifico.

POR QUE EL CATALOGO COMERCIAL NO SE PUEDE DERIVAR AUTOMATICAMENTE
------------------------------------------------------------------

Se investigo la oferta vigente de los cuatro operadores (ver
``docs/catalogo-recargas-mexico.md``) y el hallazgo fue que **los cuatro se
contradicen a si mismos en sus propias paginas oficiales**:

* AT&T publica dos tablas de GB distintas para los mismos precios; en $150 la
  diferencia es del triple.
* Movistar dice 14 GB en su PDF y 10 GB en su HTML para la recarga de $500.
* Telcel vende con el mismo precio y la misma marca dos cosas distintas segun
  sea *recarga* o *paquete*.
* Unefon presenta su oferta Ilimitado como vigente en una pagina y como
  campana vencida en otra.

Por eso ``official_verified``, ``verified_at`` y el estado ``REVIEW_REQUIRED``
no son burocracia: son lo unico que impide vender prometiendo 9 GB cuando
podrian ser 3. Un catalogo que se sincronizara solo heredaria la contradiccion
sin que nadie se enterara.

LA REGLA QUE GOBIERNA TODO EL ARCHIVO
--------------------------------------

    OFICIAL != EJECUTABLE

Que Telcel venda oficialmente un paquete no significa que nosotros podamos
entregarlo. Solo se puede cobrar cuando se cumplen las cuatro:

    official_verified
    + mapping de proveedor valido y habilitado
    + proveedor realmente disponible
    + ambiente correcto
    = SELLABLE

La comprobacion vive en ``services.disponibilidad()`` y **el estado vendible
NO se guarda en la base**. Guardarlo seria mentir el dia que el proveedor se
caiga: la fila diria AVAILABLE y el cajero cobraria algo que nadie puede
entregar. Se calcula siempre, contra el estado real del momento.
"""

from __future__ import annotations

import uuid

from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from samy_common.money import Money


class CatalogStatus(models.TextChoices):
    """Los ocho estados posibles de un producto comercial.

    Cinco los decide una persona y se guardan (ver ``ADMINISTRABLES``); los
    otros se deducen del estado real de los mappings y de los proveedores, y
    por eso nunca se persisten.
    """

    #: Vendible. Es el UNICO que permite cobrar desde caja.
    AVAILABLE = "AVAILABLE", "Disponible"
    #: No vendible por una razon que no encaja en las demas.
    UNAVAILABLE = "UNAVAILABLE", "No disponible"
    #: Producto oficial y verificado, pero ningun proveedor lo puede ejecutar.
    PROVIDER_NOT_MAPPED = "PROVIDER_NOT_MAPPED", "Sin proveedor operativo"
    #: Hay mapping, pero el proveedor no responde o no esta configurado.
    PROVIDER_OFFLINE = "PROVIDER_OFFLINE", "Proveedor fuera de linea"
    #: Apagado a mano.
    DISABLED = "DISABLED", "Deshabilitado"
    #: Se paso su fecha de vigencia comercial.
    EXPIRED = "EXPIRED", "Vencido"
    #: Algo cambio y hay que revisarlo antes de volver a venderlo.
    REVIEW_REQUIRED = "REVIEW_REQUIRED", "Requiere revision"
    #: Producto tecnico de pruebas. Nunca aparece en la caja.
    SANDBOX_ONLY = "SANDBOX_ONLY", "Solo sandbox"


#: Estados que una persona puede fijar y que por tanto se guardan.
ADMINISTRABLES: frozenset[str] = frozenset(
    {
        CatalogStatus.AVAILABLE.value,
        CatalogStatus.DISABLED.value,
        CatalogStatus.EXPIRED.value,
        CatalogStatus.REVIEW_REQUIRED.value,
        CatalogStatus.SANDBOX_ONLY.value,
    }
)


class Environment(models.TextChoices):
    SANDBOX = "SANDBOX", "Sandbox"
    PRODUCTION = "PRODUCTION", "Produccion"


class MappingStatus(models.TextChoices):
    OK = "OK", "Correcto"
    #: El producto del proveedor cambio bajo nuestros pies. No se vende hasta
    #: que alguien lo mire.
    REVIEW_REQUIRED = "REVIEW_REQUIRED", "Requiere revision"
    DISABLED = "DISABLED", "Deshabilitado"


class CommercialOperator(models.Model):
    """Compania telefonica, en los terminos de SAMY Cloud.

    Es una tabla y no un enum a proposito: la arquitectura tiene que admitir
    Bait, Virgin, Oui, FreedomPop y demas OMV sin una migracion cada vez.

    ``display_priority`` es lo que pone a Telcel primero. La aplicacion no
    sabe que Telcel es especial; sabe ordenar por prioridad. Cambiar el
    operador principal es cambiar un numero, no el codigo.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    code = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=80)
    #: Menor es primero. Telcel 10, Movistar 20, AT&T 30, Unefon 40.
    display_priority = models.PositiveSmallIntegerField(default=100)
    #: Se muestra con la etiqueta "Operador principal" en la pantalla de caja.
    is_primary = models.BooleanField(default=False)
    active = models.BooleanField(default=True, db_index=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Operador comercial"
        verbose_name_plural = "Operadores comerciales"
        ordering = ["display_priority", "name"]
        indexes = [models.Index(fields=["active", "display_priority"])]

    def __str__(self) -> str:
        return self.name


class CommercialFamily(models.Model):
    """Familia comercial dentro de un operador.

    Es la pregunta que el cajero le hace al cliente antes del monto: saldo,
    Amigo Sin Limite, Internet, otros. Cada operador tiene las suyas y no
    coinciden entre companias, asi que cuelgan del operador y no de una lista
    global.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    operator = models.ForeignKey(
        CommercialOperator, on_delete=models.PROTECT, related_name="families"
    )
    code = models.SlugField(max_length=48)
    name = models.CharField(max_length=80)
    #: Una linea para el cajero. No promete beneficios: eso vive en la version.
    description = models.CharField(max_length=160, blank=True, default="")

    display_priority = models.PositiveSmallIntegerField(default=100)
    active = models.BooleanField(default=True, db_index=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Familia comercial"
        verbose_name_plural = "Familias comerciales"
        ordering = ["operator__display_priority", "display_priority", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["operator", "code"], name="familia_unica_por_operador"
            )
        ]

    def __str__(self) -> str:
        return f"{self.operator.name} - {self.name}"


class CommercialProduct(models.Model):
    """Un producto que SAMY Cloud vende, con el nombre con el que se pide.

    El precio va en centavos enteros. Nunca float: un float de dinero acaba
    cobrando $99.99999998 y cuadrando mal la caja a fin de dia.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    operator = models.ForeignKey(
        CommercialOperator, on_delete=models.PROTECT, related_name="products"
    )
    family = models.ForeignKey(
        CommercialFamily, on_delete=models.PROTECT, related_name="products"
    )

    #: Como lo pide el cliente: "Amigo Sin Limite 100".
    commercial_name = models.CharField(max_length=120)

    price_cents = models.BigIntegerField(validators=[MinValueValidator(1)])
    currency = models.CharField(max_length=3, default="MXN")

    active = models.BooleanField(default=True, db_index=True)

    # --- Procedencia oficial ---------------------------------------------
    #
    # Sin esto no se puede vender. No es un adorno documental: es lo que
    # permite responderle a un cliente que reclama "esto no fue lo que me
    # vendieron" con la fuente y la fecha en la mano.
    official_verified = models.BooleanField(default=False, db_index=True)
    official_source = models.URLField(max_length=500, blank=True, default="")
    #: Folio del registro tarifario del IFT cuando existe. Es la fuente mas
    #: fiable que se encontro: son los documentos que el operador presenta
    #: ante el regulador y, a diferencia del HTML comercial, resultaron
    #: consistentes entre lecturas.
    official_tariff_reference = models.CharField(max_length=64, blank=True, default="")
    verified_at = models.DateTimeField(null=True, blank=True)

    display_priority = models.PositiveSmallIntegerField(default=100)

    # --- Estado ------------------------------------------------------------
    #
    # Solo los estados que decide una persona. Los que dependen del proveedor
    # se calculan en services.disponibilidad(): guardarlos dejaria filas
    # diciendo AVAILABLE mientras el proveedor esta caido.
    status = models.CharField(
        max_length=24,
        choices=CatalogStatus.choices,
        default=CatalogStatus.REVIEW_REQUIRED,
        db_index=True,
    )
    status_reason = models.CharField(max_length=200, blank=True, default="")

    #: Fin de vigencia comercial. Las promociones la tienen; los productos de
    #: catalogo normal no.
    available_until = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Producto comercial"
        verbose_name_plural = "Productos comerciales"
        ordering = [
            "operator__display_priority",
            "family__display_priority",
            "price_cents",
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["operator", "family", "commercial_name"],
                name="producto_comercial_unico",
            ),
            models.CheckConstraint(
                condition=models.Q(price_cents__gt=0),
                name="producto_comercial_precio_positivo",
            ),
            # El estado guardado solo puede ser uno de los administrables. Los
            # derivados (PROVIDER_NOT_MAPPED, PROVIDER_OFFLINE, UNAVAILABLE)
            # no se persisten nunca.
            models.CheckConstraint(
                condition=models.Q(status__in=sorted(ADMINISTRABLES)),
                name="producto_comercial_estado_administrable",
            ),
        ]
        indexes = [
            models.Index(fields=["operator", "family", "active"]),
            models.Index(fields=["active", "official_verified", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.operator.name} {self.commercial_name}"

    @property
    def price(self) -> Money:
        return Money(self.price_cents, self.currency)

    @property
    def esta_vencido(self) -> bool:
        if self.available_until is None:
            return False
        return timezone.localdate() > self.available_until

    def version_vigente(self) -> "CommercialProductVersion | None":
        return self.versions.filter(is_current=True).first()


class CommercialProductVersion(models.Model):
    """Lo que el producto ofrecia en un momento dado.

    Existe porque los operadores cambian GB, vigencias y beneficios sin avisar
    y sin renombrar el producto. Si esos datos vivieran en
    ``CommercialProduct``, actualizarlos reescribiria el pasado: un ticket de
    hace tres meses empezaria a decir que llevaba 9 GB cuando se vendio con 3.

    La orden guarda un ``snapshot()`` de la version vendida, asi que el
    comprobante historico siempre representa lo que realmente se vendio.

    Los datos van en MB enteros, no en GB decimales, por la misma razon que el
    dinero va en centavos.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    product = models.ForeignKey(
        CommercialProduct, on_delete=models.CASCADE, related_name="versions"
    )
    version = models.PositiveIntegerField()

    price_cents = models.BigIntegerField(validators=[MinValueValidator(1)])
    currency = models.CharField(max_length=3, default="MXN")

    validity_days = models.PositiveSmallIntegerField(null=True, blank=True)
    #: Datos de navegacion libre, en MB enteros.
    data_mb = models.PositiveIntegerField(null=True, blank=True)

    #: Texto tal como lo publica el operador. No se interpreta ni se resume:
    #: "Ilimitados en Mexico con destino a Estados Unidos, Canada y Puerto
    #: Rico" significa algo muy concreto y recortarlo seria prometer otra cosa.
    calls = models.CharField(max_length=200, blank=True, default="")
    sms = models.CharField(max_length=200, blank=True, default="")
    benefits = models.TextField(blank=True, default="")
    restrictions = models.TextField(blank=True, default="")

    official_source = models.URLField(max_length=500, blank=True, default="")
    official_tariff_reference = models.CharField(max_length=64, blank=True, default="")
    verified_at = models.DateTimeField(null=True, blank=True)

    #: Solo una version por producto puede estar vigente.
    is_current = models.BooleanField(default=False, db_index=True)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Version de producto comercial"
        verbose_name_plural = "Versiones de producto comercial"
        ordering = ["product", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "version"], name="version_unica_por_producto"
            ),
            models.UniqueConstraint(
                fields=["product"],
                condition=models.Q(is_current=True),
                name="una_sola_version_vigente",
            ),
            models.CheckConstraint(
                condition=models.Q(price_cents__gt=0), name="version_precio_positivo"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.product.commercial_name} v{self.version}"

    @property
    def price(self) -> Money:
        return Money(self.price_cents, self.currency)

    def snapshot(self) -> dict:
        """Copia inmutable para guardar en la orden y en el comprobante.

        Se guarda el CONTENIDO, no la referencia: si manana alguien edita esta
        version, el ticket ya emitido no cambia.
        """
        return {
            "operator": self.product.operator.code,
            "operator_name": self.product.operator.name,
            "family": self.product.family.code,
            "family_name": self.product.family.name,
            "commercial_name": self.product.commercial_name,
            "price_cents": self.price_cents,
            "currency": self.currency,
            "validity_days": self.validity_days,
            "data_mb": self.data_mb,
            "calls": self.calls,
            "sms": self.sms,
            "benefits_summary": self.benefits,
            "restrictions": self.restrictions,
            "catalog_version": self.version,
            "official_source": self.official_source,
            "official_tariff_reference": self.official_tariff_reference,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
        }


class ProviderProductMapping(models.Model):
    """Puente entre lo que vendemos y lo que el proveedor sabe ejecutar.

    Un ``CommercialProduct`` NO es el SKU del proveedor. "Telcel Amigo Sin
    Limite $100" es nuestro; el identificador que hay que mandarle a Taecel o
    a Reloadly es suyo, distinto en cada uno y distinto entre sandbox y
    produccion. Sin esta tabla habria que elegir entre acoplar el catalogo a
    un proveedor o inventarse identificadores.

    ``provider_fingerprint`` es lo que hace posible la sincronizacion segura:
    guarda como se veia el producto del proveedor la ultima vez que se
    verifico. Si la siguiente sincronizacion lo encuentra distinto -otro
    precio, otro id-, el mapping pasa a REVIEW_REQUIRED en vez de actualizarse
    solo. Un cambio silencioso aqui significa cobrar una cosa y entregar otra.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    product = models.ForeignKey(
        CommercialProduct, on_delete=models.CASCADE, related_name="mappings"
    )

    provider_slug = models.CharField(max_length=40, db_index=True)
    #: Identificador EN EL PROVEEDOR. Es lo que viaja en la peticion.
    provider_product_id = models.CharField(max_length=128)
    environment = models.CharField(
        max_length=16, choices=Environment.choices, db_index=True
    )

    enabled = models.BooleanField(default=False, db_index=True)
    status = models.CharField(
        max_length=24,
        choices=MappingStatus.choices,
        default=MappingStatus.REVIEW_REQUIRED,
    )
    status_reason = models.CharField(max_length=200, blank=True, default="")

    #: Prioridad entre proveedores para un mismo producto y ambiente. Menor
    #: primero. Lo usa ProviderRouter cuando hay mas de un candidato.
    priority = models.PositiveSmallIntegerField(default=100)

    #: Huella del producto del proveedor cuando se verifico por ultima vez.
    provider_fingerprint = models.CharField(max_length=128, blank=True, default="")
    last_verified_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Mapping de proveedor"
        verbose_name_plural = "Mappings de proveedor"
        ordering = ["product", "priority", "provider_slug"]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "provider_slug", "environment"],
                name="mapping_unico_por_producto_proveedor_ambiente",
            )
        ]
        indexes = [
            models.Index(fields=["provider_slug", "environment", "enabled"]),
        ]

    def __str__(self) -> str:
        return (
            f"{self.product.commercial_name} -> {self.provider_slug}"
            f" ({self.environment})"
        )

    @property
    def es_utilizable(self) -> bool:
        """Habilitado y sin observaciones. NO dice nada del proveedor.

        Que el mapping este bien no significa que el proveedor responda. Esas
        son dos condiciones distintas y se comprueban por separado a
        proposito: confundirlas es como acaba uno cobrando contra una API
        caida.
        """
        return self.enabled and self.status == MappingStatus.OK

    def marcar_para_revision(self, motivo: str) -> None:
        """Saca el mapping de circulacion sin borrar nada.

        Lo llama la sincronizacion cuando el producto del proveedor cambio.
        No se corrige solo: alguien tiene que mirar que paso antes de que se
        vuelva a vender.
        """
        self.status = MappingStatus.REVIEW_REQUIRED
        self.status_reason = motivo[:200]
        self.save(update_fields=["status", "status_reason", "updated_at"])
