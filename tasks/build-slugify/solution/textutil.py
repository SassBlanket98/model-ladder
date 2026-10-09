"""Small text helpers."""

import re
import unicodedata


def slugify(title: str) -> str:
    """Turn a title into a lowercase URL slug."""
    decomposed = unicodedata.normalize("NFKD", title)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
