"""Django app configuration for the users app."""

from django.apps import AppConfig


class UsersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.users"

    def ready(self) -> None:
        import apps.users.schema  # noqa: F401  # register drf-spectacular auth extension
