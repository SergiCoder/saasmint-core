"""Root URL configuration."""

from django.conf import settings
from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include, path

from apps.dashboard.views import HijackAcquireView, HijackReleaseView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("hijack/acquire/", HijackAcquireView.as_view(), name="hijack-acquire"),
    path("hijack/release/", HijackReleaseView.as_view(), name="hijack-release"),
    path("hijack/", include("hijack.urls")),
    path("dashboard/", include("apps.dashboard.urls")),
    path("api/v1/account/", include("apps.users.urls")),
    path("api/v1/billing/", include("apps.billing.urls")),
    path("api/v1/orgs/", include("apps.orgs.urls")),
    path("api/v1/webhooks/", include("apps.billing.webhook_urls")),
]

if settings.DEBUG:
    from drf_spectacular.views import (
        SpectacularAPIView,
        SpectacularRedocView,
        SpectacularSwaggerView,
    )

    urlpatterns += [
        path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
        path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
        path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    ]
    urlpatterns += staticfiles_urlpatterns()
