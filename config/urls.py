"""Root URL configuration."""

from django.conf import settings
from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.http import HttpRequest, JsonResponse
from django.urls import include, path

from apps.dashboard.views import HijackAcquireView, HijackReleaseView
from apps.users.views_references import (
    CurrencyListView,
    LocaleListView,
    PhonePrefixListView,
    TimezoneListView,
)


def health_check(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("api/v1/health/", health_check, name="health-check"),
    path("admin/", admin.site.urls),
    # Non-API admin-only views — excluded from OpenAPI schema via SCHEMA_PATH_PREFIX=/api/v[0-9].
    path("hijack/acquire/", HijackAcquireView.as_view(), name="hijack-acquire"),
    path("hijack/release/", HijackReleaseView.as_view(), name="hijack-release"),
    path("hijack/", include("hijack.urls")),
    path("dashboard/", include("apps.dashboard.urls")),
    path("api/v1/auth/", include("apps.users.auth_urls")),
    path("api/v1/account/", include("apps.users.urls")),
    path("api/v1/locales/", LocaleListView.as_view(), name="locale-list"),
    path("api/v1/currencies/", CurrencyListView.as_view(), name="currency-list"),
    path("api/v1/phone-prefixes/", PhonePrefixListView.as_view(), name="phone-prefix-list"),
    path("api/v1/timezones/", TimezoneListView.as_view(), name="timezone-list"),
    path("api/v1/billing/", include("apps.billing.urls")),
    path("api/v1/orgs/", include("apps.orgs.urls")),
    path("api/v1/marketing/", include("apps.marketing.urls")),
    path("api/v1/", include(("apps.orgs.invitation_urls", "orgs-invitations"))),
    path("api/v1/webhooks/", include("apps.billing.webhook_urls")),
]

if settings.DEBUG:
    from django.conf.urls.static import static

    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += staticfiles_urlpatterns()

# Spectacular docs can be exposed independently of DEBUG (e.g. on staging)
# via SCHEMA_PUBLIC=True. Always-on in DEBUG.
if settings.DEBUG or getattr(settings, "SCHEMA_PUBLIC", False):
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
