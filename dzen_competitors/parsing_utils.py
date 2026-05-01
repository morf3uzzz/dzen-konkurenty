import re
from typing import Optional
from urllib.parse import urlparse


_NUM_RE = re.compile(
    r"([\d][\d\s  ]*(?:[.,]\d+)?)\s*(тыс|млн|млрд|[КкKkМмMm])?",
    re.IGNORECASE,
)


def parse_count(text: Optional[str]) -> Optional[int]:
    """'1,2К просмотров' -> 1200. '55,3 тыс читали' -> 55300. '594,9 тыс' -> 594900."""
    if not text:
        return None
    t = text.replace(" ", " ").replace(" ", " ").strip()
    m = _NUM_RE.search(t)
    if not m:
        return None
    num_raw = m.group(1).replace(" ", "").replace(",", ".")
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


def parse_relative_date(text: Optional[str]) -> Optional[int]:
    """'12 часов назад' / 'Вчера' / '5 лет назад' → дней назад. None если не распознано."""
    if not text:
        return None
    t = text.lower()
    # часы/минуты → меньше суток
    if re.search(r"только что|минут[уы]?\s+назад|\d+\s*минут|час[аов]*\s*назад|\d+\s*час", t) or "сегодня" in t:
        return 0
    if "вчера" in t:
        return 1
    if "позавчера" in t:
        return 2
    m = re.search(r"(\d+)\s*(день|дня|дней|недел|месяц|мес\.|год|лет)", t)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    if unit.startswith("день") or unit.startswith("дн"):
        return n
    if unit.startswith("недел"):
        return n * 7
    if unit.startswith("месяц") or unit.startswith("мес"):
        return n * 30
    if unit.startswith("год") or unit == "лет":
        return n * 365
    return None


def parse_meta_views(text: Optional[str]) -> Optional[int]:
    """'594,9 тыс читали · 5 лет назад' → 594900. Если нет упоминания 'читал' — None."""
    if not text:
        return None
    m = re.search(r"(\d[\d\s.,]*)\s*(тыс|млн|К|M|k|m)?\s*(?:читал|прочитал|просмотр|смотрел|дочитал)", text, re.IGNORECASE)
    if not m:
        return None
    return parse_count(f"{m.group(1)} {m.group(2) or ''}".strip())


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


def canonical_article_url(url: Optional[str]) -> Optional[str]:
    """Убирает все query/fragment — оставляет только scheme+host+path.
    Дзен добавляет `rid`, `referrer_clid`, `_csrf` и т.п. — из-за них одна статья выглядит как несколько URL."""
    if not url:
        return None
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if not p.netloc or not p.path:
        return None
    return f"{p.scheme or 'https'}://{p.netloc}{p.path}"


def is_article_url(url: Optional[str]) -> bool:
    if not url:
        return False
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if "dzen.ru" not in (p.netloc or ""):
        return False
    parts = [x for x in p.path.split("/") if x]
    if not parts:
        return False
    if parts[0] in _NON_CHANNEL_ROOTS:
        return parts[0] == "a" and len(parts) >= 2
    if len(parts) >= 2 and len(parts[-1]) >= 12:
        return True
    return False
