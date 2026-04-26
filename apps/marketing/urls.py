"""URL patterns for marketing endpoints."""

from django.urls import path

from apps.marketing.views import MarketingInquiryView

urlpatterns = [
    path("inquiries/", MarketingInquiryView.as_view(), name="marketing-inquiry"),
]
