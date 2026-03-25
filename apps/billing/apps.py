from django.apps import AppConfig


class BillingConfig(AppConfig):
    name = "apps.billing"

    def ready(self) -> None:
        import stripe
        from django.conf import settings

        stripe.api_key = settings.STRIPE_SECRET_KEY
