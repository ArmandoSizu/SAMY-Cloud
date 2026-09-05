from django.urls import path

from apps.accounts import views, views_signup

app_name = "accounts"

urlpatterns = [
    path("entrar/", views.LoginView.as_view(), name="login"),
    path("salir/", views.LogoutView.as_view(), name="logout"),

    # Alta publica de un negocio. Crea SIEMPRE un STORE_OWNER: no hay campo de
    # rol en ningun formulario ni parametro que pueda cambiarlo.
    path("crear-cuenta/", views_signup.signup_account, name="signup"),
    path("crear-cuenta/negocio/", views_signup.signup_business, name="signup_business"),
    path("crear-cuenta/listo/", views_signup.signup_done, name="signup_done"),

    path("recuperar/", views.PasswordResetView.as_view(), name="password_reset"),
    path("recuperar/enviado/", views.PasswordResetDoneView.as_view(), name="password_reset_done"),
    path(
        "recuperar/<uidb64>/<token>/",
        views.PasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path("recuperar/listo/", views.PasswordResetCompleteView.as_view(), name="password_reset_complete"),
    path("contrasena/", views.PasswordChangeView.as_view(), name="password_change"),
    path("tienda/<uuid:store_id>/activar/", views.switch_store, name="switch_store"),
]
