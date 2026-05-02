import re
from typing import Optional
from urllib.parse import urlparse


_NUM_RE = re.compile(
    r"([\d][\d\s   ]*(?:[.,]\d+)?)\s*(тыс|млн|млрд|[КкKkМмMm])?",
    re.IGNORECASE,
)


def parse_count(text: Optional[str]) -> Optional[int]:
    """'1,2К просмотров' -> 1200. '15 320' (с NBSP) -> 15320."""
    if not text:
        return None
    # Дзен использует U+00A0 (NBSP), U+2009 (thin space), U+202F (narrow nbsp).
    t = text
    for ch in (" ", " ", " "):
        t = t.replace(ch, " ")
    t = t.strip()
    m = _NUM_RE.search(t)
    if not m:
        return None
    num_raw = m.group(1)
    for ch in (" ", " ", " ", " "):
        num_raw = num_raw.replace(ch, "")
    num_raw = num_raw.replace(",", ".")
    suffix = (m.group(2) or "").lower()
    try:
        num = float(num_raw)
    except ValueError:
        return None
    mult = 1
    if suffix in ("к", "k", "тыс"):
        mult = 1_000
    elif suffix in ("м", "m", "млн"):
        mult = 1_000_000
    elif suffix == "млрд":
        mult = 1_000_000_000
    return int(num * mult)


_NON_CHANNEL_ROOTS = {
    "a", "video", "media", "search", "suggest", "topic",
    "profile", "settings", "news", "tag", "live", "shorts",
}


def channel_slug_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if "dzen.ru" not in (p.netloc or ""):
        return None
    parts = [x for x in p.path.split("/") if x]
    if not parts:
        return None
    if parts[0] == "id" and len(parts) >= 2:
        return f"id/{parts[1]}"
    if parts[0] in _NON_CHANNEL_ROOTS:
        return None
    return parts[0]
