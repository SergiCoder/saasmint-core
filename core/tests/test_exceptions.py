"""Tests for domain exceptions."""

from __future__ import annotations

import pytest

from saasmint_core.exceptions import (
    AccountTypeConflictError,
    DomainError,
    InsufficientPermissionError,
    InvalidPromoCodeError,
    OrgMemberNotFoundError,
    OrgNotFoundError,
    SubscriptionAlreadyActiveError,
    SubscriptionNotFoundError,
    UserNotFoundError,
    WebhookDataError,
    WebhookVerificationError,
)

_ALL_EXCEPTIONS = [
    UserNotFoundError,
    OrgNotFoundError,
    SubscriptionNotFoundError,
    SubscriptionAlreadyActiveError,
    AccountTypeConflictError,
    InvalidPromoCodeError,
    InsufficientPermissionError,
    OrgMemberNotFoundError,
    WebhookVerificationError,
    WebhookDataError,
]


def test_all_exceptions_inherit_domain_error() -> None:
    for exc_class in _ALL_EXCEPTIONS:
        assert issubclass(exc_class, DomainError)


def test_domain_error_inherits_exception() -> None:
    assert issubclass(DomainError, Exception)


def test_each_exception_can_be_raised_and_caught() -> None:
    for exc_class in _ALL_EXCEPTIONS:
        with pytest.raises(DomainError):
            raise exc_class("test message")


def test_exception_message_preserved() -> None:
    err = UserNotFoundError("user 123 not found")
    assert str(err) == "user 123 not found"


def test_webhook_verification_error() -> None:
    with pytest.raises(WebhookVerificationError):
        raise WebhookVerificationError("bad signature")


def test_invalid_promo_code_error() -> None:
    with pytest.raises(InvalidPromoCodeError):
        raise InvalidPromoCodeError("code SAVE20 is expired")
