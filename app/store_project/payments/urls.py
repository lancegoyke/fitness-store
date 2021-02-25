from django.urls import path

from . import views

app_name = "payments"
urlpatterns = [
    path(
        "login-to-purchase/<str:product_type>/<str:product_slug>/",
        views.login_to_purchase,
        name="login_to_purchase",
    ),
    path("config/", views.stripe_config, name="stripe_config"),
    path(
        "create-checkout-session/",
        views.create_checkout_session,
        name="stripe_create_checkout_session",
    ),
    path("success/", views.SuccessView.as_view(), name="success"),
    path("webhook/", views.stripe_webhook, name="webhook"),
]
