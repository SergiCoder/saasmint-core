"""Project-wide template context processors."""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest


def app_context(request: HttpRequest) -> dict[str, object]:
    """Expose ENVIRONMENT and the docs-links gate to every template.

    `schema_links_enabled` mirrors the URL-registration gate in `config/urls.py`
    so the admin template can never render a link to a route that wasn't
    registered (which would raise NoReverseMatch).
    """
    return {
        "ENVIRONMENT": settings.ENVIRONMENT,
        "schema_links_enabled": settings.DEBUG or settings.SCHEMA_PUBLIC,
    }
