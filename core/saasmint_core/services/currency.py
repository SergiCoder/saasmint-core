"""Display-currency helpers used by the billing serializers.

The catalog is USD-only and Stripe is always charged in USD. ``format_amount``
converts minor units to a display float; ``round_friendly`` snaps a converted
amount to a charm price (``.49``/``.99`` for two-decimal currencies; nearest
10/100 for zero-decimal). Both run at sync time inside
``apps.billing.tasks.sync_localized_prices`` — request-path serializers
just read the precomputed ``LocalizedPrice`` rows.
"""

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

ZERO_DECIMAL_CURRENCIES: frozenset[str] = frozenset({"jpy", "krw", "idr", "twd"})

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
    """Convert minor units to display amount. JPY/KRW/IDR are zero-decimal."""
    if currency.lower() in ZERO_DECIMAL_CURRENCIES:
        return float(amount)
    return amount / 100


def round_friendly(amount: float, currency: str) -> float:
    """Round a display amount to the nearest user-friendly number.

    Standard currencies snap to the closest of ``.49`` or ``.99`` — which
    may round *down* (17.23 → 16.99, 4.55 → 4.49) as well as up (19.77 →
    19.99). Losing a few cents of display precision is fine; we just want
    a clean price tag.

    Zero-decimal currencies (JPY, KRW, …) round to the nearest 10 below
    1000 and to the nearest 100 at or above 1000.
    """
    if amount <= 0:
        return 0.0

    if currency.lower() in ZERO_DECIMAL_CURRENCIES:
        step = 100 if amount >= 1000 else 10
        return float(round(amount / step) * step)

    whole = int(amount)
    # Anchors span the previous, current, and next whole-unit bands so the
    # nearest pick can legitimately round down across a boundary.
    options = [
        whole - 0.01,  # (whole-1).99
        whole + 0.49,
        whole + 0.99,
        whole + 1.49,
    ]
    return min(options, key=lambda o: abs(o - amount))
