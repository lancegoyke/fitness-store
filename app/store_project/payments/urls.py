from django.urls import path

from . import views

app_name = "payments"
urlpatterns = [
    path(
        "login-to-purchase/<str:product_slug>/",
        views.login_before_purchase,
        name="login_to_purchase",
    ),
    path("config/", views.stripe_config),
    path("create-checkout-session/", views.create_checkout_session),
    path("success/", views.SuccessView.as_view(), name="success"),
    path("cancellation/", views.CancellationView.as_view(), name="cancellation"),
    path("webhook/", views.stripe_webhook),
]
