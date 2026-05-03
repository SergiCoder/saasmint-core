"""Marketing API views — public, unauthenticated inquiry intake."""

from __future__ import annotations

import logging
from typing import ClassVar

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import BaseThrottle
from rest_framework.views import APIView

from apps.marketing.email import redact_email
from apps.marketing.serializers import MarketingInquirySerializer
from apps.marketing.tasks import send_marketing_inquiry_email_task
from apps.marketing.throttling import MarketingInquiryThrottle

logger = logging.getLogger(__name__)


class MarketingInquiryView(APIView):
    """POST /api/v1/marketing/inquiries/ — forward landing-page CTA / Contact form to admin inbox.

    Throttled on its own ``marketing_inquiries`` scope rather than the shared ``auth`` scope: the
    failure mode here (admin inbox filled by one IP) is more direct than auth's (outbound spam),
    and the traffic shape is one submission per visitor rather than bursty.
    """

    permission_classes: ClassVar[list[type[BasePermission]]] = [AllowAny]  # type: ignore[misc]
    throttle_classes: ClassVar[list[type[BaseThrottle]]] = [MarketingInquiryThrottle]  # type: ignore[misc]

    @extend_schema(
        request=MarketingInquirySerializer,
        responses={204: None},
        tags=["marketing"],
    )
    def post(self, request: Request) -> Response:
        ser = MarketingInquirySerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        # Silent honeypot drop — bots can't differentiate from a real success.
        if data.get("honeypot"):
            return Response(status=status.HTTP_204_NO_CONTENT)

        to_address = settings.MARKETING_INQUIRIES_TO.strip()
        if not to_address:
            logger.error("MARKETING_INQUIRIES_TO is not configured; cannot forward inquiry")
            return Response(
                {
                    "detail": "Marketing inquiry inbox is not configured.",
                    "code": "marketing_inbox_unconfigured",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        send_marketing_inquiry_email_task.delay(
            to=to_address,
            source=data["source"],
            sender=data["email"],
            message=data.get("message", ""),
        )
        logger.info(
            "Marketing inquiry accepted (source=%s, from=%s)",
            data["source"],
            redact_email(data["email"]),
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
