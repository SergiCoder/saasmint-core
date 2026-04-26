"""Serializers for marketing endpoints."""

from __future__ import annotations

from rest_framework import serializers

INQUIRY_SOURCES = ("landing-cta", "contact-page")
INQUIRY_MESSAGE_MAX = 5000
INQUIRY_EMAIL_MAX = 254  # RFC 5321


class MarketingInquirySerializer(serializers.Serializer[object]):
    email = serializers.EmailField(max_length=INQUIRY_EMAIL_MAX)
    message = serializers.CharField(
        max_length=INQUIRY_MESSAGE_MAX,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        default="",
    )
    # `source` collides with DRF Field's built-in `source` attribute in stubs;
    # the assignment is correct at runtime, so silence the spurious mypy error.
    source = serializers.ChoiceField(choices=INQUIRY_SOURCES)  # type: ignore[assignment]
    honeypot = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        if attrs["source"] == "contact-page" and not attrs.get("message"):
            raise serializers.ValidationError(
                {"message": "This field is required for contact-page inquiries."}
            )
        return attrs
