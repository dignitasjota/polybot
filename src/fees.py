"""Polymarket fee calculation (May 2026).

New fee structure: category-based taker fees + maker rebates.
Makers never pay fees. Takers pay: feeRate × shares × p × (1-p).

Reference: https://docs.polymarket.com/trading/fees
"""

from __future__ import annotations

# Taker fee rates by category
TAKER_FEE_RATES: dict[str, float] = {
    "crypto": 0.072,
    "sports": 0.03,
    "finance": 0.04,
    "politics": 0.04,
    "tech": 0.04,
    "mentions": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "other": 0.05,
    "geopolitics": 0.0,
}

# Maker rebate as fraction of taker fee
MAKER_REBATE_RATES: dict[str, float] = {
    "crypto": 0.20,
    "sports": 0.25,
    "finance": 0.25,
    "politics": 0.25,
    "tech": 0.25,
    "mentions": 0.25,
    "economics": 0.25,
    "culture": 0.25,
    "weather": 0.25,
    "other": 0.25,
    "geopolitics": 0.0,
}

# Default category when unknown
DEFAULT_CATEGORY = "other"

# Gas cost for redeeming positions
GAS_REDEEM_USD = 0.004


def taker_fee(price: float, shares: float, category: str = "crypto") -> float:
    """Calculate taker fee: feeRate × shares × p × (1-p).

    Args:
        price: Price per share (0-1)
        shares: Number of shares
        category: Market category (crypto, sports, politics, etc.)

    Returns:
        Fee in USDC
    """
    rate = TAKER_FEE_RATES.get(category, TAKER_FEE_RATES[DEFAULT_CATEGORY])
    return rate * shares * price * (1.0 - price)


def taker_fee_per_share(price: float, category: str = "crypto") -> float:
    """Fee per share for takers: feeRate × p × (1-p)."""
    rate = TAKER_FEE_RATES.get(category, TAKER_FEE_RATES[DEFAULT_CATEGORY])
    return rate * price * (1.0 - price)


def maker_rebate(price: float, shares: float, category: str = "crypto") -> float:
    """Calculate maker rebate earned when a taker fills your order.

    Rebate = rebate_rate × taker_fee.
    """
    fee = taker_fee(price, shares, category)
    rebate_rate = MAKER_REBATE_RATES.get(category, MAKER_REBATE_RATES[DEFAULT_CATEGORY])
    return fee * rebate_rate


def category_from_tags(tags: list[str]) -> str:
    """Determine fee category from market tags.

    Polymarket tags map to fee categories. First matching tag wins.
    """
    for tag in tags:
        tag = tag.lower()
        if tag in TAKER_FEE_RATES:
            return tag
        # Common tag aliases
        if tag in ("geopolitical",):
            return "geopolitics"
        if tag in ("political", "elections"):
            return "politics"
        if tag in ("technology", "ai"):
            return "tech"
        if tag in ("financial", "markets"):
            return "finance"
        if tag in ("economic",):
            return "economics"
        if tag in ("sport", "nfl", "nba", "mlb", "soccer", "football"):
            return "sports"
        if tag in ("cryptocurrency", "bitcoin", "ethereum", "defi"):
            return "crypto"
    return DEFAULT_CATEGORY


# Mapping from Gamma API feeType field to our fee categories
# The keyset endpoint returns feeType instead of feeSchedule
FEE_TYPE_MAP: dict[str, str] = {
    "crypto_fees_v2": "crypto",
    "sports_fees_v2": "sports",
    "politics_fees": "politics",
    "general_fees": "politics",      # general = finance/politics (0.04)
    "culture_fees": "culture",
    "weather_fees": "weather",
}


def fee_rate_from_fee_type(fee_type: str | None, fees_enabled: bool = True) -> float:
    """Map Gamma API feeType to a taker fee rate.

    Args:
        fee_type: Value of the "feeType" field from keyset endpoint (e.g. "crypto_fees_v2")
        fees_enabled: Value of the "feesEnabled" field

    Returns:
        Taker fee rate (e.g. 0.072 for crypto), or -1.0 if unknown.
    """
    if not fees_enabled:
        return 0.0
    if not fee_type:
        return -1.0
    category = FEE_TYPE_MAP.get(fee_type)
    if category is None:
        return -1.0
    return TAKER_FEE_RATES.get(category, TAKER_FEE_RATES[DEFAULT_CATEGORY])


def category_from_fee_type(fee_type: str | None, fees_enabled: bool = True) -> str:
    """Map Gamma API feeType to our fee category name.

    Returns DEFAULT_CATEGORY if unknown.
    """
    if not fees_enabled:
        return "geopolitics"
    if not fee_type:
        return DEFAULT_CATEGORY
    return FEE_TYPE_MAP.get(fee_type, DEFAULT_CATEGORY)


def net_margin(price: float, category: str = "crypto") -> float:
    """Net margin per share after fees and gas: (1-p) - fee_per_share - gas."""
    return (1.0 - price) - taker_fee_per_share(price, category) - GAS_REDEEM_USD
