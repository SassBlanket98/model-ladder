"""Quote prices. All amounts are whole cents."""

PRICES = {
    "WID-1": 1250,
    "WID-2": 2400,
    "GAD-7": 9900,
    "BOLT-3": 35,
}


def line_total(sku: str, qty: int) -> int:
    """Price of one order line in cents."""
    if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
        raise ValueError(f"quantity must be a positive integer, got {qty!r}")
    return PRICES[sku] * qty


def order_total(lines: list[tuple[str, int]]) -> int:
    """Price of a whole order: a list of (sku, quantity) lines."""
    return sum(line_total(sku, qty) for sku, qty in lines)
