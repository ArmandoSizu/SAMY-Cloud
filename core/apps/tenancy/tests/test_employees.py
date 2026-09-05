"""Pruebas del panel de empleados y del aislamiento entre negocios.

Las dos preguntas que responde este archivo:

1. ¿Puede un cajero hacer algo que no le corresponde?
2. ¿Puede un dueno ver o tocar el negocio de otro?

Si alguna respuesta fuera "si", el producto no se puede vender.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from apps.accounts.services import register_owner
from apps.tenancy.models import Membership, Role

User = get_user_model()

CONTRASENA_DUENO = "Tortilla-Caliente-2026"


def _crear_negocio(sufijo: str):
    """Crea un negocio completo con su propietario, como haria el registro."""
    from django.contrib.auth.hashers import make_password

    return register_owner(
        email=f"dueno{sufijo}@negocio.mx",
        password_hash=make_password(CONTRASENA_DUENO),
        first_name="Dueno",
        last_name=f"Numero {sufijo}",
        phone="3121234567",
        organization_name=f"Organizacion {sufijo}",
        store_name=f"Tienda {sufijo}",
        city="Colima",
        state="Colima",
        postal_code="28000",
    )


class EmployeeManagementTests(TestCase):
    """8. Creacion de CASHIER por STORE_OWNER."""

    def setUp(self) -> None:
        cache.clear()
        self.negocio = _crear_negocio("A")
        self.client.force_login(self.negocio.user)

    def _alta_cajero(self, email: str = "cajero@negocio.mx", **extra):
        datos = {
            "first_name": "Luis",
            "last_name": "Perez",
            "email": email,
            "phone": "3121112233",
            **extra,
        }
        return self.client.post(reverse("employees:new"), datos)

    def test_el_dueno_crea_un_cajero(self) -> None:
        respuesta = self._alta_cajero()

        self.assertRedirects(respuesta, reverse("employees:list"))

        membresia = Membership.objects.get(user__email="cajero@negocio.mx")
        self.assertEqual(membresia.role, Role.CASHIER)
        self.assertEqual(membresia.store_id, self.negocio.store.id)
        self.assertTrue(membresia.is_active)

    def test_el_cajero_nace_obligado_a_cambiar_la_contrasena(self) -> None:
        """La clave temporal la conoce el dueno; no puede quedarse puesta."""
        self._alta_cajero()

        cajero = User.objects.get(email="cajero@negocio.mx")
        self.assertTrue(cajero.must_change_password)
        self.assertFalse(cajero.is_platform_admin)
        self.assertFalse(cajero.is_staff)

    def test_la_clave_temporal_se_muestra_una_sola_vez(self) -> None:
        self._alta_cajero()

        primera = self.client.get(reverse("employees:list"))
        self.assertContains(primera, "Contrasena temporal")

        segunda = self.client.get(reverse("employees:list"))
        self.assertNotContains(segunda, "Contrasena temporal")

    def test_la_clave_temporal_funciona_y_lleva_al_cambio_obligatorio(self) -> None:
        """Prueba de punta a punta de la credencial que se entrega al cajero."""
        self._alta_cajero()
        credencial = self.client.session["samy_credencial_temporal"]
        self.client.get(reverse("employees:list"))  # la consume
        self.client.logout()

        # Por la pantalla real: django-axes exige una peticion para contar
        # intentos, y asi se prueba el camino que recorre el cajero.
        entrada = self.client.post(
            reverse("accounts:login"),
            {"username": credencial["email"], "password": credencial["password"]},
        )
        self.assertEqual(
            entrada.status_code,
            302,
            "la clave temporal entregada no sirve para entrar",
        )

        # El middleware lo encierra en la pantalla de cambio de contrasena.
        respuesta = self.client.get(reverse("dashboard:home"))
        self.assertRedirects(respuesta, reverse("accounts:password_change"))

    def test_el_dueno_no_puede_crear_un_administrador_de_plataforma(self) -> None:
        """El rol no se lee del POST ni aqui.

        Aunque el formulario venga con role=PLATFORM_ADMIN, create_cashier
        solo sabe crear CASHIER.
        """
        self._alta_cajero(role="PLATFORM_ADMIN", is_platform_admin="true", is_staff="1")

        cajero = User.objects.get(email="cajero@negocio.mx")
        self.assertFalse(cajero.is_platform_admin)
        self.assertFalse(cajero.is_staff)
        self.assertEqual(
            Membership.objects.get(user=cajero).role, Role.CASHIER
        )

    def test_desactivar_y_reactivar_a_un_cajero(self) -> None:
        self._alta_cajero()
        membresia = Membership.objects.get(user__email="cajero@negocio.mx")

        self.client.post(reverse("employees:toggle", args=[membresia.id]))
        membresia.refresh_from_db()
        self.assertFalse(membresia.is_active)
        # El USUARIO sigue activo: puede trabajar en otra tienda.
        self.assertTrue(membresia.user.is_active)

        self.client.post(reverse("employees:toggle", args=[membresia.id]))
        membresia.refresh_from_db()
        self.assertTrue(membresia.is_active)

    def test_el_dueno_no_puede_desactivarse_a_si_mismo(self) -> None:
        """Se quedaria fuera de su propio negocio sin nadie que lo devuelva."""
        propia = Membership.objects.get(user=self.negocio.user)

        self.client.post(reverse("employees:toggle", args=[propia.id]))

        propia.refresh_from_db()
        self.assertTrue(propia.is_active)

    def test_restablecer_acceso_genera_una_clave_nueva(self) -> None:
        self._alta_cajero()
        membresia = Membership.objects.get(user__email="cajero@negocio.mx")
        anterior = membresia.user.password
        self.client.get(reverse("employees:list"))  # consume la credencial

        self.client.post(reverse("employees:reset", args=[membresia.id]))

        membresia.user.refresh_from_db()
        self.assertNotEqual(membresia.user.password, anterior)
        self.assertTrue(membresia.user.must_change_password)

        nueva = self.client.session["samy_credencial_temporal"]
        self.assertTrue(membresia.user.check_password(nueva["password"]))

    def test_no_se_da_de_alta_dos_veces_en_la_misma_tienda(self) -> None:
        self._alta_cajero()
        self.client.get(reverse("employees:list"))

        respuesta = self._alta_cajero()

        self.assertEqual(respuesta.status_code, 422)
        self.assertEqual(
            Membership.objects.filter(user__email="cajero@negocio.mx").count(), 1
        )

    def test_la_lista_muestra_la_fecha_de_alta(self) -> None:
        self._alta_cajero()
        respuesta = self.client.get(reverse("employees:list"))

        self.assertContains(respuesta, "Alta ")
        self.assertContains(respuesta, "nunca ha entrado")


class CashierPermissionTests(TestCase):
    """9. Un cajero no tiene permisos administrativos."""

    def setUp(self) -> None:
        cache.clear()
        self.negocio = _crear_negocio("B")

        self.client.force_login(self.negocio.user)
        self.client.post(
            reverse("employees:new"),
            {
                "first_name": "Luis",
                "last_name": "Perez",
                "email": "cajero@negocio.mx",
                "phone": "3121112233",
            },
        )
        self.client.get(reverse("employees:list"))
        self.client.logout()

        self.cajero = User.objects.get(email="cajero@negocio.mx")
        # Se limpia la marca para poder probar los permisos y no quedarse
        # atrapado en la pantalla de cambio de contrasena.
        self.cajero.must_change_password = False
        self.cajero.save(update_fields=["must_change_password"])
        self.client.force_login(self.cajero)

    def test_el_cajero_no_entra_a_empleados(self) -> None:
        respuesta = self.client.get(reverse("employees:list"))
        self.assertEqual(respuesta.status_code, 403)

    def test_el_cajero_no_puede_dar_de_alta_a_nadie(self) -> None:
        respuesta = self.client.post(
            reverse("employees:new"),
            {
                "first_name": "Otro",
                "last_name": "Mas",
                "email": "colado@negocio.mx",
                "phone": "3121112244",
            },
        )
        self.assertEqual(respuesta.status_code, 403)
        self.assertFalse(User.objects.filter(email="colado@negocio.mx").exists())

    def test_el_cajero_no_puede_desactivar_al_dueno(self) -> None:
        del_dueno = Membership.objects.get(user=self.negocio.user)

        respuesta = self.client.post(
            reverse("employees:toggle", args=[del_dueno.id])
        )

        self.assertEqual(respuesta.status_code, 403)
        del_dueno.refresh_from_db()
        self.assertTrue(del_dueno.is_active)

    def test_el_cajero_no_ve_el_enlace_de_empleados(self) -> None:
        respuesta = self.client.get(reverse("dashboard:home"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertNotContains(respuesta, reverse("employees:list"))

    def test_los_permisos_del_cajero_son_los_declarados(self) -> None:
        membresia = Membership.objects.get(user=self.cajero)

        self.assertTrue(membresia.has_perm("operation.create"))
        self.assertTrue(membresia.has_perm("operation.view_own"))

        for prohibido in (
            "store.manage_employees",
            "store.manage_settings",
            "store.view_commissions",
            "store.view_reports",
            "operation.view_store",
            "operation.refund",
            "platform.view_all_stores",
        ):
            self.assertFalse(
                membresia.has_perm(prohibido),
                f"el cajero no deberia tener '{prohibido}'",
            )

    def test_al_desactivarlo_deja_de_operar_en_la_siguiente_peticion(self) -> None:
        """No se espera a que caduque la sesion: el middleware revalida siempre."""
        membresia = Membership.objects.get(user=self.cajero)
        membresia.is_active = False
        membresia.save(update_fields=["is_active"])

        respuesta = self.client.get(reverse("dashboard:home"))

        # Sigue autenticado, pero ya no tiene tienda: no puede operar.
        self.assertEqual(respuesta.status_code, 200)
        self.assertIsNone(respuesta.wsgi_request.store)
        self.assertIsNone(respuesta.wsgi_request.membership)


class TenantIsolationTests(TestCase):
    """10. Aislamiento entre organizaciones."""

    def setUp(self) -> None:
        cache.clear()
        self.negocio_a = _crear_negocio("A")
        self.negocio_b = _crear_negocio("B")

        # El negocio B da de alta a su cajero.
        self.client.force_login(self.negocio_b.user)
        self.client.post(
            reverse("employees:new"),
            {
                "first_name": "Ana",
                "last_name": "Lopez",
                "email": "ana@negociob.mx",
                "phone": "3121115566",
            },
        )
        self.client.get(reverse("employees:list"))
        self.client.logout()

        self.cajero_de_b = Membership.objects.get(user__email="ana@negociob.mx")

    def test_el_dueno_de_a_no_ve_a_los_empleados_de_b(self) -> None:
        self.client.force_login(self.negocio_a.user)

        respuesta = self.client.get(reverse("employees:list"))

        self.assertEqual(respuesta.status_code, 200)
        self.assertNotContains(respuesta, "ana@negociob.mx")
        self.assertNotContains(respuesta, self.negocio_b.user.email)

    def test_el_dueno_de_a_no_puede_desactivar_a_un_cajero_de_b(self) -> None:
        """Y recibe 404, no 403.

        Con un 403 se confirmaria que ese identificador existe, que ya es
        informacion sobre el negocio ajeno. Con 404 no se filtra nada.
        """
        self.client.force_login(self.negocio_a.user)

        respuesta = self.client.post(
            reverse("employees:toggle", args=[self.cajero_de_b.id])
        )

        self.assertEqual(respuesta.status_code, 404)
        self.cajero_de_b.refresh_from_db()
        self.assertTrue(self.cajero_de_b.is_active)

    def test_el_dueno_de_a_no_puede_restablecer_el_acceso_de_un_cajero_de_b(self) -> None:
        self.client.force_login(self.negocio_a.user)
        anterior = self.cajero_de_b.user.password

        respuesta = self.client.post(
            reverse("employees:reset", args=[self.cajero_de_b.id])
        )

        self.assertEqual(respuesta.status_code, 404)
        self.cajero_de_b.user.refresh_from_db()
        self.assertEqual(self.cajero_de_b.user.password, anterior)

    def test_cada_negocio_tiene_su_propia_organizacion_y_tienda(self) -> None:
        self.assertNotEqual(
            self.negocio_a.organization_id
            if hasattr(self.negocio_a, "organization_id")
            else self.negocio_a.organization.id,
            self.negocio_b.organization.id,
        )
        self.assertNotEqual(self.negocio_a.store.id, self.negocio_b.store.id)
        self.assertNotEqual(
            self.negocio_a.store.organization_id,
            self.negocio_b.store.organization_id,
        )
