"""Pruebas del alta publica de un negocio.

Lo que se comprueba aqui no es que "el formulario funcione": es que no exista
ningun camino por el que el registro publico produzca privilegios que no debe
producir, ni deje la cuenta a medio crear.

Se usa el corredor de pruebas de Django y no pytest porque ``pytest-django``
no esta instalado en la imagen. ``django.test.TestCase`` envuelve cada prueba
en una transaccion que se deshace al terminar, asi que ninguna toca la base de
desarrollo.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, identify_hasher
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from apps.audit.models import AuditAction, AuditEvent
from apps.tenancy.models import Membership, Organization, Role, Store

User = get_user_model()

PASO_1 = {
    "first_name": "Sofia",
    "last_name": "Ramirez Luna",
    "email": "sofia@abarroteslaesperanza.mx",
    "phone": "3121234567",
    "password1": "Tortilla-Caliente-2026",
    "password2": "Tortilla-Caliente-2026",
}

PASO_2 = {
    "store_name": "Abarrotes La Esperanza",
    "organization_name": "Grupo Esperanza",
    "store_phone": "3129876543",
    "city": "Colima",
    "state": "Colima",
    "postal_code": "28000",
}


class SignupTests(TestCase):
    """El camino feliz y sus limites."""

    def setUp(self) -> None:
        # El limitador guarda contadores en cache y la cache es Redis, que
        # NO se deshace con la transaccion de la prueba. Sin limpiarla, la
        # quinta prueba de la clase chocaria con el limite de la primera.
        cache.clear()

    def _registrar(self, paso1: dict | None = None, paso2: dict | None = None):
        self.client.post(reverse("accounts:signup"), paso1 or PASO_1)
        return self.client.post(reverse("accounts:signup_business"), paso2 or PASO_2)

    # -- 1. registro exitoso de propietario ------------------------------

    def test_registro_exitoso_crea_al_propietario(self) -> None:
        respuesta = self._registrar()

        self.assertRedirects(respuesta, reverse("accounts:signup_done"))

        usuario = User.objects.get(email=PASO_1["email"])
        self.assertEqual(usuario.first_name, "Sofia")
        self.assertEqual(usuario.last_name, "Ramirez Luna")
        # El telefono se guarda normalizado a 10 digitos, no como se escribio.
        self.assertEqual(usuario.phone, "3121234567")
        self.assertTrue(usuario.is_active)

        # La contrasena quedo cifrada y sirve para entrar. Que el hash no sea
        # el texto plano es lo minimo; lo que importa es que AUTENTIQUE.
        self.assertNotEqual(usuario.password, PASO_1["password1"])
        self.assertTrue(usuario.check_password(PASO_1["password1"]))

    # -- 2. creacion automatica de Organization --------------------------

    def test_registro_crea_la_organizacion(self) -> None:
        self._registrar()

        organizacion = Organization.objects.get(name="Grupo Esperanza")
        self.assertTrue(organizacion.is_active)
        self.assertEqual(organizacion.slug, "grupo-esperanza")

    def test_dos_negocios_con_el_mismo_nombre_no_chocan(self) -> None:
        """El slug es unico en toda la plataforma.

        "Abarrotes La Esperanza" hay muchos en Mexico. Sin desambiguar, el
        segundo registro reventaria con un error de clave duplicada.
        """
        self._registrar()

        self.client.logout()
        cache.clear()
        segundo_paso1 = {**PASO_1, "email": "otro@negocio.mx"}
        self._registrar(paso1=segundo_paso1)

        slugs = list(
            Organization.objects.filter(name="Grupo Esperanza")
            .order_by("created_at")
            .values_list("slug", flat=True)
        )
        self.assertEqual(len(slugs), 2)
        self.assertEqual(len(set(slugs)), 2, f"slugs repetidos: {slugs}")

    # -- 3. creacion automatica de Store ---------------------------------

    def test_registro_crea_la_tienda_ligada_a_la_organizacion(self) -> None:
        self._registrar()

        tienda = Store.objects.get(name="Abarrotes La Esperanza")
        self.assertEqual(tienda.organization.name, "Grupo Esperanza")
        self.assertEqual(tienda.city, "Colima")
        self.assertEqual(tienda.postal_code, "28000")
        self.assertTrue(tienda.is_active)

        # El codigo sale impreso en el folio del comprobante, asi que tiene
        # que cumplir el formato desde el primer momento.
        self.assertRegex(tienda.code, r"^[A-Z0-9\-]{3,12}$")

    # -- 4. Membership STORE_OWNER ---------------------------------------

    def test_registro_crea_la_membresia_de_propietario(self) -> None:
        self._registrar()

        membresia = Membership.objects.get(user__email=PASO_1["email"])
        self.assertEqual(membresia.role, Role.STORE_OWNER)
        self.assertTrue(membresia.is_active)
        # Predeterminada, para que al entrar ya tenga tienda activa y no caiga
        # en la pantalla de "no tienes ninguna tienda asignada".
        self.assertTrue(membresia.is_default)
        self.assertTrue(membresia.has_perm("store.manage_employees"))

    def test_el_alta_queda_en_la_bitacora(self) -> None:
        self._registrar()

        evento = AuditEvent.objects.filter(action=AuditAction.EMPLOYEE_ADDED).first()
        self.assertIsNotNone(evento, "el alta no dejo registro de auditoria")
        self.assertEqual(evento.new_state, "STORE_OWNER")
        self.assertEqual(evento.metadata.get("motivo"), "alta_publica")

    # -- 5. rechazo de email duplicado -----------------------------------

    def test_email_duplicado_se_rechaza(self) -> None:
        User.objects.create_user(
            email=PASO_1["email"], password="Otra-Contrasena-Larga-99"
        )

        respuesta = self.client.post(reverse("accounts:signup"), PASO_1)

        self.assertEqual(respuesta.status_code, 422)
        self.assertContains(respuesta, "Ya existe una cuenta", status_code=422)
        # Y sobre todo: no se creo nada.
        self.assertEqual(User.objects.filter(email=PASO_1["email"]).count(), 1)
        self.assertFalse(Organization.objects.exists())

    # -- 6. rechazo de contrasena invalida -------------------------------

    def test_contrasena_corta_se_rechaza(self) -> None:
        datos = {**PASO_1, "password1": "corta12", "password2": "corta12"}
        respuesta = self.client.post(reverse("accounts:signup"), datos)

        self.assertEqual(respuesta.status_code, 422)
        self.assertFalse(User.objects.filter(email=PASO_1["email"]).exists())

    def test_contrasenas_que_no_coinciden_se_rechazan(self) -> None:
        datos = {**PASO_1, "password2": "Tortilla-Caliente-2027"}
        respuesta = self.client.post(reverse("accounts:signup"), datos)

        self.assertEqual(respuesta.status_code, 422)
        self.assertContains(respuesta, "no coinciden", status_code=422)
        self.assertFalse(User.objects.filter(email=PASO_1["email"]).exists())

    def test_contrasena_igual_al_correo_se_rechaza(self) -> None:
        """Usar el propio correo como contrasena es de los errores mas comunes."""
        datos = {
            **PASO_1,
            "password1": PASO_1["email"],
            "password2": PASO_1["email"],
        }
        respuesta = self.client.post(reverse("accounts:signup"), datos)

        self.assertEqual(respuesta.status_code, 422)
        self.assertFalse(User.objects.filter(email=PASO_1["email"]).exists())

    def test_telefono_invalido_se_rechaza(self) -> None:
        datos = {**PASO_1, "phone": "12345"}
        respuesta = self.client.post(reverse("accounts:signup"), datos)

        self.assertEqual(respuesta.status_code, 422)
        self.assertFalse(User.objects.filter(email=PASO_1["email"]).exists())

    # -- 7. imposibilidad de registrar PLATFORM_ADMIN --------------------

    def test_no_se_puede_registrar_un_administrador_de_plataforma(self) -> None:
        """Aunque el POST venga cargado de campos que piden privilegios.

        Es la prueba que mas importa de este archivo: ninguna combinacion de
        parametros del registro publico puede producir un administrador. El
        rol no se lee de la peticion en ningun punto del camino.
        """
        paso1_malicioso = {
            **PASO_1,
            "role": Role.PLATFORM_ADMIN,
            "is_platform_admin": "true",
            "is_staff": "true",
            "is_superuser": "on",
        }
        paso2_malicioso = {
            **PASO_2,
            "role": "PLATFORM_ADMIN",
            "is_platform_admin": "1",
        }

        self._registrar(paso1=paso1_malicioso, paso2=paso2_malicioso)

        usuario = User.objects.get(email=PASO_1["email"])
        self.assertFalse(usuario.is_platform_admin, "se creo un administrador")
        self.assertFalse(usuario.is_staff)
        self.assertFalse(usuario.is_superuser)

        membresia = Membership.objects.get(user=usuario)
        self.assertEqual(membresia.role, Role.STORE_OWNER)
        self.assertFalse(membresia.has_perm("platform.manage_providers"))

    def test_no_se_puede_registrar_un_cajero_libremente(self) -> None:
        """El registro publico tampoco crea cajeros: a esos los da de alta el dueno."""
        self._registrar(paso1={**PASO_1, "role": Role.CASHIER})

        membresia = Membership.objects.get(user__email=PASO_1["email"])
        self.assertEqual(membresia.role, Role.STORE_OWNER)

    # -- Atomicidad ------------------------------------------------------

    def test_el_paso_2_no_deja_usuario_sin_tienda(self) -> None:
        """Si el paso 2 no se completa, no debe quedar un usuario huerfano.

        Un usuario sin organizacion ni tienda entra a un panel del que no
        puede salir: no puede operar y tampoco puede crear su negocio.
        """
        self.client.post(reverse("accounts:signup"), PASO_1)

        # Se abandona el paso 2 con datos invalidos.
        respuesta = self.client.post(
            reverse("accounts:signup_business"), {**PASO_2, "postal_code": "abc"}
        )

        self.assertEqual(respuesta.status_code, 422)
        self.assertFalse(User.objects.filter(email=PASO_1["email"]).exists())
        self.assertFalse(Organization.objects.exists())
        self.assertFalse(Store.objects.exists())

    def test_no_se_puede_saltar_al_paso_2 (self) -> None:
        """Entrar directo al paso 2 sin haber hecho el 1 no crea nada."""
        respuesta = self.client.post(reverse("accounts:signup_business"), PASO_2)

        self.assertRedirects(respuesta, reverse("accounts:signup"))
        self.assertFalse(Store.objects.exists())

    # -- Sesion ----------------------------------------------------------

    def test_el_registro_no_deja_la_sesion_iniciada(self) -> None:
        """Decision deliberada: un POST de registro nunca devuelve sesion.

        Asi un alta forzada desde otro sitio no puede dejar a alguien dentro
        de una cuenta ajena en su propio navegador.
        """
        self._registrar()

        respuesta = self.client.get(reverse("dashboard:home"))
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn(reverse("accounts:login"), respuesta["Location"])

    def test_la_contrasena_no_viaja_en_claro_entre_pantallas(self) -> None:
        """Entre el paso 1 y el 2 la sesion solo guarda el hash.

        La comprobacion es sobre la propiedad, no sobre el nombre del
        algoritmo: la suite corre con un hasher rapido a proposito (ver
        config/settings/test.py) y afirmar "empieza por argon2" comprobaria
        la configuracion de las pruebas en vez del comportamiento del
        registro. Lo que importa es que en la sesion no quede la contrasena
        en claro y que lo guardado sea un hash valido de esa contrasena.
        """
        self.client.post(reverse("accounts:signup"), PASO_1)

        pendiente = self.client.session["samy_signup_pending"]
        self.assertNotIn("password1", pendiente)
        self.assertNotIn(PASO_1["password1"], str(pendiente))

        guardado = pendiente["password_hash"]
        # identify_hasher revienta si el valor no tiene forma de hash Django.
        self.assertIsNotNone(identify_hasher(guardado))
        self.assertTrue(check_password(PASO_1["password1"], guardado))
        self.assertFalse(check_password("otra-cosa-distinta", guardado))

    # -- Acceso ----------------------------------------------------------

    def test_el_propietario_recien_creado_entra_a_su_panel(self) -> None:
        self._registrar()

        # Se entra por la pantalla real y no con client.login(): django-axes
        # exige una peticion para contar los intentos, y ademas asi se prueba
        # el camino que recorre la persona, no un atajo del framework.
        entrada = self.client.post(
            reverse("accounts:login"),
            {"username": PASO_1["email"], "password": PASO_1["password1"]},
        )
        self.assertEqual(
            entrada.status_code, 302, "el propietario no pudo iniciar sesion"
        )

        respuesta = self.client.get(reverse("dashboard:home"))
        self.assertEqual(respuesta.status_code, 200)
        # Panel de dueno: incluye el acceso a Empleados, que un cajero no ve.
        self.assertContains(respuesta, reverse("employees:list"))

    def test_el_enlace_de_crear_cuenta_esta_en_el_login(self) -> None:
        respuesta = self.client.get(reverse("accounts:login"))
        self.assertContains(respuesta, reverse("accounts:signup"))
        self.assertContains(respuesta, "Crear cuenta")
