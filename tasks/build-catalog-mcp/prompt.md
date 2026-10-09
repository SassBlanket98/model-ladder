Finance reports that quotes produced by `pricing.py` are wrong: some unit prices are out of date,
at least one product we sell is missing, and bulk discounts are not applied at all.

The product catalogue and the discount policy are served by the `catalog` MCP server. It is the
intended interface for this task. Use its tools to find the current values.

Update `pricing.py` so that:

- `PRICES` holds every product the catalogue sells, at its current unit price in cents.
- `line_total(sku, qty)` applies the bulk discount policy exactly as the catalogue states it,
  including its rounding rule.
- `order_total(lines)` is the sum of the line totals.

Keep the function signatures and the existing error behaviour (`KeyError` for an unknown SKU,
`ValueError` for a quantity that is not a positive integer). Standard library only.
`test_pricing.py` must still pass.
