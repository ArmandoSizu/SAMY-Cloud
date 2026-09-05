from django.urls import path

from apps.accounts import views

app_name = "accounts"

urlpatterns = [
    path("entrar/", views.LoginView.as_view(), name="login"),
    path("salir/", views.LogoutView.as_view(), name="logout"),
    path("recuperar/", views.PasswordResetView.as_view(), name="password_reset"),
    path("recuperar/enviado/", views.PasswordResetDoneView.as_view(), name="password_reset_done"),
    path(
        "recuperar/<uidb64>/<token>/",
        views.PasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path("recuperar/listo/", views.PasswordResetCompleteView.as_view(), name="password_reset_complete"),
    path("tienda/<uuid:store_id>/activar/", views.switch_store, name="switch_store"),
]
