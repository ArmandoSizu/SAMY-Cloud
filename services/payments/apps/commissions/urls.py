from django.urls import path

from apps.commissions import views

app_name = "commissions"

urlpatterns = [
    path("preview/", views.preview, name="preview"),
    path("rules/", views.rules, name="rules"),
]
