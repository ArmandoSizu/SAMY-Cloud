"""Modelo de usuario de SAMY Cloud.

Se define un ``User`` propio desde el primer dia. Cambiar ``AUTH_USER_MODEL``
despues de la primera migracion es una de las operaciones mas dolorosas de
Django, y en un sistema con transacciones historicas es practicamente
irreversible.

Diferencias frente al usuario por defecto:

* **El correo es el identificador**, no un "username". Un cajero no deberia
  inventarse un nombre de usuario; ademas el correo es lo que se usa para
  recuperar la contrasena.
* **UUID como clave primaria**: un id autoincremental filtra cuantos usuarios
  tiene la plataforma y facilita enumeracion.
* **Rol resuelto por tienda**, no global: ver ``apps.tenancy.models.Membership``.
"""

from __future__ import annotations

import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class UserManager(BaseUserManager):
    """Gestor que crea usuarios usando el correo como identificador."""

    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra):
        if not email:
            raise ValueError("El correo electronico es obligatorio.")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra)
        # ``set_password`` aplica Argon2; nunca se guarda texto plano.
        user.set_password(password)
        user.full_clean(exclude=["password"])
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_platform_admin", True)
        extra.setdefault("is_active", True)
        if extra.get("is_staff") is not True:
            raise ValueError("Un superusuario debe tener is_staff=True.")
        if extra.get("is_superuser") is not True:
            raise ValueError("Un superusuario debe tener is_superuser=True.")
        return self._create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    """Usuario de SAMY Cloud."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    email = models.EmailField(
        _("correo electronico"),
        unique=True,
        error_messages={"unique": "Ya existe una cuenta con este correo."},
    )
    first_name = models.CharField(_("nombre"), max_length=80, blank=True, default="")
    last_name = models.CharField(_("apellidos"), max_length=80, blank=True, default="")
    phone = models.CharField(max_length=20, blank=True, default="")

    #: Administrador de SAMY Cloud (la plataforma), distinto de dueno de tienda.
    is_platform_admin = models.BooleanField(
        default=False,
        help_text="Acceso al panel de administracion de la plataforma.",
    )
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    date_joined = models.DateTimeField(default=timezone.now)
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)
    #: Obliga a cambiar la contrasena en el siguiente inicio de sesion.
    must_change_password = models.BooleanField(default=False)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        verbose_name = "Usuario"
        verbose_name_plural = "Usuarios"
        ordering = ["email"]
        indexes = [models.Index(fields=["is_active", "is_platform_admin"])]

    def __str__(self) -> str:
        return self.email

    # -- presentacion ---------------------------------------------------

    @property
    def full_name(self) -> str:
        name = f"{self.first_name} {self.last_name}".strip()
        return name or self.email.split("@")[0]

    def get_full_name(self) -> str:
        return self.full_name

    def get_short_name(self) -> str:
        return self.first_name or self.email.split("@")[0]

    @property
    def initials(self) -> str:
        """Iniciales para el avatar del navbar."""
        parts = [p for p in (self.first_name, self.last_name) if p]
        if parts:
            return "".join(p[0].upper() for p in parts[:2])
        return self.email[:2].upper()

    # -- ambito multi-tenant --------------------------------------------

    def active_memberships(self):
        from apps.tenancy.models import Membership

        return (
            Membership.objects.filter(user=self, is_active=True, store__is_active=True)
            .select_related("store", "store__organization")
            .order_by("-is_default", "store__name")
        )

    def default_membership(self):
        """Membresia con la que se abre sesion."""
        return self.active_memberships().first()

    def membership_for(self, store_id):
        return self.active_memberships().filter(store_id=store_id).first()

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.lower().strip()
        super().save(*args, **kwargs)
