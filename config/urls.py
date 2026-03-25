"""Root URL configuration."""

from django.conf import settings
from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("hijack/", include("hijack.urls")),
    path("api/v1/account/", include("apps.users.urls")),
    path("api/v1/billing/", include("apps.billing.urls")),
    path("api/v1/webhooks/", include("apps.billing.webhook_urls")),
]

if settings.DEBUG:
    urlpatterns += staticfiles_urlpatterns()  # type: ignore[arg-type]
