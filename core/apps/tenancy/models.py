"""Modelo multi-tenant de SAMY Cloud.

Jerarquia:

    Organization (el cliente que contrata SAMY Cloud)
      └── Store (cada sucursal fisica o punto de venta)
            └── Membership (que usuario trabaja aqui y con que rol)

Estrategia de aislamiento elegida: **discriminador por fila** (``store_id`` en
cada tabla de negocio) en vez de un esquema o base de datos por inquilino.

Por que:

* Un esquema por tienda multiplica las migraciones por el numero de clientes;
  con cientos de tiendas cada despliegue se vuelve una operacion de horas.
* Una base por tienda impide consultas agregadas de plataforma sin ETL.
* El discriminador por fila escala a miles de inquilinos y es el modelo que
  usan la mayoria de los SaaS B2B de este tamano.

El riesgo del discriminador por fila es olvidar el filtro en una consulta y
filtrar datos de otra tienda. Ese riesgo se mitiga en tres capas:

1. ``StoreScopedManager`` obliga a pasar por ``for_user()`` o ``for_store()``.
2. ``StoreScopedQuerysetMixin`` en las vistas aplica el filtro siempre.
3. Pruebas de aislamiento que verifican que la tienda A no ve datos de la B.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.core.validators import MinLengthValidator, RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


class Role(models.TextChoices):
    """Roles del sistema (RBAC).

    Se define como ``TextChoices`` y no como grupos de Django porque los
    permisos de SAMY Cloud dependen del par (usuario, tienda), no solo del
    usuario: la misma persona puede ser dueno en una tienda y cajero en otra.
    """

    PLATFORM_ADMIN = "PLATFORM_ADMIN", "Administrador de plataforma"
    STORE_OWNER = "STORE_OWNER", "Propietario del negocio"
    CASHIER = "CASHIER", "Cajero"


#: Permisos concretos por rol. Se declara explicitamente en vez de deducirlo
#: por jerarquia para que sea auditable de un vistazo.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.PLATFORM_ADMIN: frozenset(
        {
            "platform.view_all_stores",
            "platform.manage_providers",
            "platform.view_audit",
            "platform.manage_plans",
            "store.view_dashboard",
            "store.manage_settings",
            "store.manage_employees",
            "store.view_reports",
            "store.view_commissions",
            "operation.create",
            "operation.view_own",
            "operation.view_store",
            "operation.refund",
        }
    ),
    Role.STORE_OWNER: frozenset(
        {
            "store.view_dashboard",
            "store.manage_settings",
            "store.manage_employees",
            "store.view_reports",
            "store.view_commissions",
            "operation.create",
            "operation.view_own",
            "operation.view_store",
            "operation.refund",
        }
    ),
    Role.CASHIER: frozenset(
        {
            # Un cajero opera y ve LO SUYO. No entra a configuracion, no ve
            # comisiones ni reportes de la tienda, no puede reembolsar.
            "store.view_dashboard",
            "operation.create",
            "operation.view_own",
        }
    ),
}


class TimeStampedModel(models.Model):
    """Base con marcas de tiempo. Toda tabla de negocio las necesita."""

    created_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Organization(TimeStampedModel):
    """Entidad que contrata SAMY Cloud. Puede tener varias tiendas."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150, validators=[MinLengthValidator(3)])
    slug = models.SlugField(max_length=160, unique=True)

    #: RFC mexicano. Se valida el formato, no la existencia ante el SAT.
    tax_id = models.CharField(
        max_length=13,
        blank=True,
        default="",
        validators=[
            RegexValidator(
                regex=r"^([A-ZÑ&]{3,4})\d{6}([A-Z\d]{3})$",
                message="El RFC no tiene un formato valido.",
            )
        ],
        help_text="RFC. Solo se valida el formato.",
    )

    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Organizacion"
        verbose_name_plural = "Organizaciones"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:160]
        super().save(*args, **kwargs)


class Store(TimeStampedModel):
    """Punto de venta. Es la unidad de aislamiento de datos del sistema."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="stores"
    )
    name = models.CharField(max_length=150)
    #: Codigo corto que aparece en el folio del comprobante.
    code = models.CharField(
        max_length=12,
        validators=[
            RegexValidator(
                regex=r"^[A-Z0-9\-]{3,12}$",
                message="Solo mayusculas, digitos y guiones (3-12 caracteres).",
            )
        ],
    )

    address = models.CharField(max_length=255, blank=True, default="")
    city = models.CharField(max_length=100, blank=True, default="")
    state = models.CharField(max_length=100, blank=True, default="")
    postal_code = models.CharField(max_length=5, blank=True, default="")
    phone = models.CharField(max_length=20, blank=True, default="")

    timezone_name = models.CharField(max_length=64, default="America/Mexico_City")
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Tienda"
        verbose_name_plural = "Tiendas"
        ordering = ["organization__name", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"], name="store_code_unique_per_org"
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class Membership(TimeStampedModel):
    """Relacion usuario-tienda con un rol. Es donde vive el RBAC real.

    Una persona puede tener varias membresias: dueno en su tienda y cajero en
    la de un familiar. El rol efectivo siempre se resuelve contra la tienda
    activa, nunca de forma global.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=32, choices=Role.choices)

    is_active = models.BooleanField(default=True)
    #: Tienda que se abre por defecto al iniciar sesion.
    is_default = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Membresia"
        verbose_name_plural = "Membresias"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "store"], name="membership_unique_user_store"
            ),
            # Como maximo una membresia marcada como predeterminada por usuario.
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(is_default=True),
                name="membership_single_default_per_user",
            ),
        ]
        indexes = [models.Index(fields=["user", "is_active"])]

    def __str__(self) -> str:
        return f"{self.user} @ {self.store} ({self.get_role_display()})"

    @property
    def permissions(self) -> frozenset[str]:
        return ROLE_PERMISSIONS.get(self.role, frozenset())

    def has_perm(self, permission: str) -> bool:
        return permission in self.permissions


class StoreScopedQuerySet(models.QuerySet):
    """QuerySet que exige un ambito de tienda explicito.

    No se sobreescribe ``get_queryset`` para filtrar automaticamente porque un
    filtro implicito y silencioso es peor que uno explicito: cuando falla, nadie
    se entera. Aqui el llamador declara el ambito y eso es revisable en el
    codigo y en las pruebas.
    """

    def for_store(self, store: Store | uuid.UUID | str) -> "StoreScopedQuerySet":
        store_id = store.id if isinstance(store, Store) else store
        return self.filter(store_id=store_id)

    def for_organization(self, organization: Organization) -> "StoreScopedQuerySet":
        return self.filter(store__organization=organization)

    def for_user(self, user) -> "StoreScopedQuerySet":
        """Limita a las tiendas donde el usuario tiene membresia activa.

        Un administrador de plataforma ve todo; cualquier otro rol solo ve
        aquello a lo que pertenece.
        """
        if not user.is_authenticated:
            return self.none()
        if user.is_platform_admin:
            return self
        return self.filter(
            store__memberships__user=user, store__memberships__is_active=True
        ).distinct()


class StoreScopedModel(TimeStampedModel):
    """Base de toda tabla que contiene datos de un inquilino."""

    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="+")

    objects = StoreScopedQuerySet.as_manager()

    class Meta:
        abstract = True
