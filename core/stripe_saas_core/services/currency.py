"""Currency resolution — determines which currency to use for a user's checkout."""

SUPPORTED_CURRENCIES: frozenset[str] = frozenset(
    {
        "usd",
        "eur",
        "gbp",
        "jpy",
        "brl",
        "krw",
        "sek",
        "nok",
        "dkk",
        "pln",
        "try",
        "idr",
        "rub",
        "cny",
        "twd",
        "sar",
        "aed",
        "chf",
        "cad",
        "aud",
    }
)

ZERO_DECIMAL_CURRENCIES: frozenset[str] = frozenset({"jpy", "krw", "idr"})

# Maps ISO 3166-1 alpha-2 country codes to currency codes
COUNTRY_CURRENCY_MAP: dict[str, str] = {
    "US": "usd",
    "CA": "cad",
    "AU": "aud",
    "GB": "gbp",
    "DE": "eur",
    "FR": "eur",
    "IT": "eur",
    "ES": "eur",
    "NL": "eur",
    "PT": "eur",
    "FI": "eur",
    "AT": "eur",
    "BE": "eur",
    "IE": "eur",
    "GR": "eur",
    "SK": "eur",
    "JP": "jpy",
    "CN": "cny",
    "TW": "twd",
    "KR": "krw",
    "BR": "brl",
    "SE": "sek",
    "NO": "nok",
    "DK": "dkk",
    "PL": "pln",
    "TR": "try",
    "ID": "idr",
    "RU": "rub",
    "SA": "sar",
    "AE": "aed",
    "CH": "chf",
}


def resolve_currency(
    preferred: str | None = None,
    billing_country: str | None = None,
    accept_language: str | None = None,
) -> str:
    """
    Resolve the currency to use for a checkout session.

    Priority:
      1. User's explicit preference (users.preferred_currency)
      2. Billing country from Stripe Customer
      3. Country inferred from Accept-Language header
      4. Default: usd
    """
    if preferred and preferred.lower() in SUPPORTED_CURRENCIES:
        return preferred.lower()

    if billing_country:
        currency = COUNTRY_CURRENCY_MAP.get(billing_country.upper())
        if currency:
            return currency

    if accept_language:
        currency = _currency_from_accept_language(accept_language)
        if currency:
            return currency

    return "usd"


def _currency_from_accept_language(accept_language: str) -> str | None:
    """Extract a country code from Accept-Language and map to currency."""
    for part in accept_language.split(","):
        tag = part.split(";")[0].strip()
        if "-" in tag:
            country = tag.split("-")[-1].upper()
            currency = COUNTRY_CURRENCY_MAP.get(country)
            if currency:
                return currency
    return None


def format_amount(amount: int, currency: str) -> float:
    """Convert minor units to display amount. JPY/KRW are zero-decimal."""
    if currency.lower() in ZERO_DECIMAL_CURRENCIES:
        return float(amount)
    return amount / 100
