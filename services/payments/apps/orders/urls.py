from django.urls import path

from apps.orders import views

app_name = "orders"

urlpatterns = [
    path("", views.create_order, name="create"),
    path("summary/today/", views.daily_summary, name="daily_summary"),
    path("history/", views.order_history, name="history"),
    path("<uuid:order_id>/", views.order_detail, name="detail"),
    path("<uuid:order_id>/pay/", views.start_payment, name="start_payment"),
    path("<uuid:order_id>/confirm-cash/", views.confirm_cash, name="confirm_cash"),
]
