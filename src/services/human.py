"""Parsing of human-formatted numbers ("1,2 mi", "1.2M", "12.345", "1:23").

Social platforms render counts differently per locale, so all of the ambiguity
has to be resolved here rather than in each adapter.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

MULTIPLIERS: dict[str, int] = {
    "k": 1_000,
    "mil": 1_000,
    "thousand": 1_000,
    "m": 1_000_000,
    "mi": 1_000_000,
    "mn": 1_000_000,
    "million": 1_000_000,
    "milhao": 1_000_000,
    "milhoes": 1_000_000,
    "milhão": 1_000_000,
    "milhões": 1_000_000,
    "b": 1_000_000_000,
    "bi": 1_000_000_000,
    "bn": 1_000_000_000,
    "billion": 1_000_000_000,
    "bilhao": 1_000_000_000,
    "bilhoes": 1_000_000_000,
    "bilhão": 1_000_000_000,
    "bilhões": 1_000_000_000,
    "t": 1_000_000_000_000,
    "tri": 1_000_000_000_000,
}

NUMBER_RE = re.compile(
    r"(?P<number>\d{1,3}(?:[.,\u00a0\u202f ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    r"\s*(?P<suffix>[a-zA-ZÀ-ÿ]{0,10})"
)

THOUSANDS_RE = re.compile(r"^\d{1,3}(?:[.,\u00a0\u202f ]\d{3})+$")

_SUFFIX_STRIP = str.maketrans("", "", ".")


def _normalize_suffix(suffix: str | None) -> str:
    if not suffix:
        return ""
    return suffix.strip().lower().translate(_SUFFIX_STRIP)


def _to_decimal(raw: str) -> Decimal | None:
    """Convert a localized numeric string into a Decimal.

    Rules:
    * both separators present -> the right-most one is the decimal separator;
    * a single separator followed by exactly 3 digits, repeated, is a thousands
      separator (``1.234.567`` / ``1,234,567``);
    * a single separator otherwise is a decimal separator.
    """
    cleaned = raw.replace("\u00a0", "").replace("\u202f", "").replace(" ", "").strip()
    if not cleaned:
        return None

    has_dot = "." in cleaned
    has_comma = "," in cleaned

    if has_dot and has_comma:
        if cleaned.rfind(".") > cleaned.rfind(","):
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(".", "").replace(",", ".")
    elif THOUSANDS_RE.match(cleaned):
        # "1.234" / "1,234" -> thousand separators (pt-BR reading, the primary locale)
        cleaned = cleaned.replace(".", "").replace(",", "")
    elif has_comma:
        cleaned = cleaned.replace(",", ".")
    elif has_dot and cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")

    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def parse_count(value: str | int | float | None) -> int | None:
    """Parse a human-readable count into an int.

    >>> parse_count("1.2M")
    1200000
    >>> parse_count("1,2 mi")
    1200000
    >>> parse_count("12.345")
    12345
    >>> parse_count("1,234")
    1234
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))

    text = str(value).strip()
    if not text:
        return None

    match = NUMBER_RE.search(text)
    if not match:
        return None

    number = _to_decimal(match.group("number"))
    if number is None:
        return None

    suffix = _normalize_suffix(match.group("suffix"))
    multiplier = MULTIPLIERS.get(suffix, 1)
    return int(number * multiplier)


def parse_percent(value: str | float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = re.search(r"(-?\d+(?:[.,]\d+)?)\s*%", str(value))
    if not match:
        return None
    number = _to_decimal(match.group(1))
    return float(number) if number is not None else None


_ISO_DURATION_RE = re.compile(
    r"P(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?"
)


def parse_duration(value: str | int | float | None) -> float | None:
    """Parse ISO-8601 (``PT15M51S``), clock (``1:23``) or seconds into seconds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    if text.upper().startswith("P"):
        match = _ISO_DURATION_RE.fullmatch(text.upper())
        if not match:
            return None
        parts = {key: float(val) if val else 0.0 for key, val in match.groupdict().items()}
        return (
            parts["days"] * 86400
            + parts["hours"] * 3600
            + parts["minutes"] * 60
            + parts["seconds"]
        )

    if ":" in text:
        chunks = text.split(":")
        try:
            numbers = [float(chunk.replace(",", ".")) for chunk in chunks]
        except ValueError:
            return None
        total = 0.0
        for number in numbers:
            total = total * 60 + number
        return total

    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def clean_text(value: str | None) -> str | None:
    """Collapse whitespace and decode the entities that show up in og-tags."""
    if value is None:
        return None
    import html as html_lib

    text = html_lib.unescape(value)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


_RELATIVE_UNITS: dict[str, int] = {
    "second": 1,
    "segundo": 1,
    "segundos": 1,
    "seconds": 1,
    "minute": 60,
    "minuto": 60,
    "minutos": 60,
    "minutes": 60,
    "hour": 3600,
    "hora": 3600,
    "horas": 3600,
    "hours": 3600,
    "day": 86400,
    "dia": 86400,
    "dias": 86400,
    "days": 86400,
    "week": 604800,
    "semana": 604800,
    "semanas": 604800,
    "weeks": 604800,
    "month": 2_592_000,
    "mês": 2_592_000,
    "mes": 2_592_000,
    "meses": 2_592_000,
    "months": 2_592_000,
    "year": 31_536_000,
    "ano": 31_536_000,
    "anos": 31_536_000,
    "years": 31_536_000,
}

_RELATIVE_RE = re.compile(
    r"(?:(?:h[áa]|il y a)\s+)?(?P<count>\d+)\s*(?P<unit>[a-zA-ZÀ-ÿ]+)"
    r"(?:\s*(?:ago|atrás|atras))?",
    re.IGNORECASE,
)


def parse_relative_date(text: str | None, *, now: datetime | None = None) -> datetime | None:
    """Turn "2 days ago" / "há 3 semanas" / "Streamed 1 year ago" into UTC."""
    if not text:
        return None
    lowered = text.lower()
    if not any(marker in lowered for marker in ("ago", "atr", "há ", "ha ")):
        return None
    match = _RELATIVE_RE.search(text)
    if not match:
        return None
    seconds = _RELATIVE_UNITS.get(match.group("unit").lower())
    if seconds is None:
        return None
    base = now or datetime.now(UTC)
    return base - timedelta(seconds=int(match.group("count")) * seconds)
