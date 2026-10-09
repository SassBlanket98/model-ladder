"""Small text helpers."""


def slugify(title: str) -> str:
    """Turn a title into a lowercase URL slug."""
    out = []
    for ch in title.lower():
        if ch.isascii() and ch.isalnum():
            out.append(ch)
        elif ch.isascii():
            out.append("-")
    return "".join(out)
