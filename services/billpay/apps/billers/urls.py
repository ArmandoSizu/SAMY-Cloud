from django.urls import path

from apps.billers import views

app_name = "billers"

urlpatterns = [
    path("", views.billers, name="list"),
    path("inquire/", views.inquire, name="inquire"),
]
