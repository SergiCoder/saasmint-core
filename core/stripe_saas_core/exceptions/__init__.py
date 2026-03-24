"""Domain exceptions — backends map these to HTTP status codes."""


class DomainError(Exception):
    """Base class for all domain errors."""


class UserNotFoundError(DomainError):
    """No user found with the given identifier."""


class OrgNotFoundError(DomainError):
    """No org found with the given identifier."""


class SubscriptionNotFoundError(DomainError):
    """No subscription found for this customer."""


class SubscriptionAlreadyActiveError(DomainError):
    """User already has an active subscription."""


class AccountTypeConflictError(DomainError):
    """User tried to switch billing context without cancelling existing subscription."""


class InvalidPromoCodeError(DomainError):
    """Promo code does not exist, is expired, or has reached its usage limit."""


class InsufficientPermissionError(DomainError):
    """User does not have the required org role to perform this action."""


class OrgMemberNotFoundError(DomainError):
    """User is not a member of this org."""


class WebhookVerificationError(DomainError):
    """Stripe webhook signature verification failed."""
