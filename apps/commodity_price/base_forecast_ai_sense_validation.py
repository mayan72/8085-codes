import re

import pandas as pd


_SHORT_TERM_HEADING_RE = re.compile(
    r"(?im)^\s*\*{0,2}Short-term Outlook\*{0,2}\s*:?"
)
_LONG_TERM_HEADING_RE = re.compile(
    r"(?im)^\s*\*{0,2}Long-term Outlook\*{0,2}\s*:?"
)
_OMEGA_RE = re.compile(r"Ω")


def base_ai_sense_has_short_term_omega(value):
    """True when base_ai_sense has Short-term Outlook and an Ω bullet in that section."""
    if value is None or (isinstance(value, float) and pd.isnull(value)) or pd.isnull(value):
        return False

    text = str(value)
    short_match = _SHORT_TERM_HEADING_RE.search(text)
    if not short_match:
        return False

    rest = text[short_match.end():]
    long_match = _LONG_TERM_HEADING_RE.search(rest)
    short_section = rest[: long_match.start()] if long_match else rest
    return bool(_OMEGA_RE.search(short_section))
