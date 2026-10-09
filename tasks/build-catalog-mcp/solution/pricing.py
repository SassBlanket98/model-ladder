"""Quote prices. All amounts are whole cents."""

PRICES = {
    "WID-1": 1250,
    "WID-2": 2650,
    "GAD-7": 10450,
    "BOLT-3": 35,
    "NUT-4": 18,
}

# (minimum quantity, percent off), highest tier first.
DISCOUNT_TIERS = ((50, 12), (10, 5))


def line_total(sku: str, qty: int) -> int:
    """Price of one order line in cents, after the bulk discount for its quantity."""
    if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
        raise ValueError(f"quantity must be a positive integer, got {qty!r}")
    gross = PRICES[sku] * qty
    percent_off = next((pct for minimum, pct in DISCOUNT_TIERS if qty >= minimum), 0)
    # Half-up rounding to whole cents in integer arithmetic.
    return (gross * (100 - percent_off) * 2 + 100) // 200


def order_total(lines: list[tuple[str, int]]) -> int:
    """Price of a whole order: a list of (sku, quantity) lines."""
    return sum(line_total(sku, qty) for sku, qty in lines)
