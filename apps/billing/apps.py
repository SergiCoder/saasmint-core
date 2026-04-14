from django.apps import AppConfig


class BillingConfig(AppConfig):
    name = "apps.billing"

    def ready(self) -> None:
        import stripe
        from django.conf import settings

        stripe.api_key = settings.STRIPE_SECRET_KEY
        # Pin the API version so upgrades to stripe-python don't silently
        # change webhook payload shapes on us. Bump this deliberately after
        # reviewing the Stripe changelog and updating any affected code paths.
        stripe.api_version = "2026-03-25.dahlia"
