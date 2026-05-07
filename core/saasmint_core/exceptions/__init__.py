"""Domain exceptions — backends map these to HTTP status codes."""


class DomainError(Exception):
    """Base class for all domain errors."""


class UserNotFoundError(DomainError):
    """No user found with the given identifier."""


class OrgNotFoundError(DomainError):
    """No org found with the given identifier."""


class SubscriptionNotFoundError(DomainError):
    """No subscription found for this customer."""


class SeatsBelowMemberCountError(DomainError):
    """Requested seat count is below the org's current member count."""


class InsufficientPermissionError(DomainError):
    """User does not have the required org role to perform this action."""


class WebhookVerificationError(DomainError):
    """Stripe webhook signature verification failed."""


class WebhookDataError(DomainError):
    """Webhook event references unknown entities (customer, price, etc.)."""
