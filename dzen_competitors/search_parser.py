"""Сбор каналов из поиска Дзена через Playwright.
Два источника:
1) Вкладка "Каналы" (type_filter=publisher) — основной источник.
2) Карточки статей в общем поиске (type_filter=article,brief) — дополнительный.
"""
import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

from playwright.async_api import BrowserContext

from browser import polite_sleep, scroll_until_idle
from config import Config
from parsing_utils import channel_slug_from_url, parse_count

logger = logging.getLogger(__name__)


@dataclass
class ChannelHit:
    slug: str
    url: str
    title: Optional[str]
    description: Optional[str]
    subscribers: Optional[int]
    source: str            # 'search_publisher' | 'search_article' | 'similar'
    source_query: Optional[str]


PUBLISHER_CARD = '[data-testid="search-publisher-card"]'
ARTICLE_CARD = '[data-card-type="card-article"]'

JS_PUBLISHER = r"""
(cards) => cards.map(card => {
    const pickHref = sel => { const e = card.querySelector(sel); return e ? e.href : null; };
    const pickText = sel => { const e = card.querySelector(sel); return e ? (e.textContent || '').trim() : null; };
    return {
        link:
            pickHref('a[class*="search-title__titleWrapper"]') ||
            pickHref('a[class*="safe-link__link"]') ||
            pickHref('a[aria-label="Название канала"]'),
        title: pickText('[class*="search-vital-card__title"]')
            || pickText('[class*="search-title__title"]'),
        description: pickText('[class*="search-vital-card-header__subtitleText"]')
            || pickText('[class*="search-vital-card-header__subtitle"]'),
        meta: pickText('[class*="search-vital-card-header__subscribers"]'),
    };
});
"""

JS_ARTICLE_CHANNELS = r"""
(cards) => cards.map(card => {
    const pickHref = sel => { const e = card.querySelector(sel); return e ? e.href : null; };
    const pickText = sel => { const e = card.querySelector(sel); return e ? (e.textContent || '').trim() : null; };
    return {
        channel_link: pickHref('a[class*="card-author__authorTitleLink"]')
            || pickHref('a[class*="card-author__avatarLink"]'),
        channel_title: pickText('a[class*="card-author__authorTitleLink"]'),
    };
});
"""


async def _detect_captcha(page) -> bool:
    """Возвращает True, если страница похожа на показ капчи Дзена/Яндекса."""
    try:
        title = (await page.title() or "").lower()
        url = page.url.lower()
        if "captcha" in url or "showcaptcha" in url or "smartcaptcha" in url:
            return True
        if "captcha" in title or "подтвердите" in title:
            return True
    except Exception:
        pass
    return False


async def search_channels_publisher(
    ctx: BrowserContext, query: str, cfg: Config,
) -> list[ChannelHit]:
    """Источник 1: вкладка "Каналы" — публикатор-карточки."""
    url = f"https://dzen.ru/search?query={quote(query)}&type_filter=publisher"
    page = await ctx.new_page()
    hits: list[ChannelHit] = []
    try:
        logger.info("[поиск-каналы] %s", query)
        await page.goto(url, wait_until="domcontentloaded")
        await polite_sleep(cfg.min_delay, cfg.max_delay)
        if await _detect_captcha(page):
            logger.warning("[поиск-каналы] '%s': КАПЧА — Дзен заблокировал. Сделай паузу или используй прокси.", query)
            return hits
        try:
            await page.wait_for_selector(PUBLISHER_CARD, timeout=12000)
        except Exception:
            logger.warning("[поиск-каналы] '%s': карточек нет", query)
            return hits
        await scroll_until_idle(
            page,
            max_scrolls=cfg.max_scrolls_search,
            idle_rounds=cfg.scroll_idle_rounds,
            item_selector=PUBLISHER_CARD,
            min_delay=cfg.min_delay,
            max_delay=cfg.max_delay,
        )
        raw = await page.eval_on_selector_all(PUBLISHER_CARD, JS_PUBLISHER)
        seen: set[str] = set()
        for it in raw:
            link = it.get("link") or ""
            slug = channel_slug_from_url(link)
            if not slug or slug in seen:
                continue
            seen.add(slug)
            meta = it.get("meta") or ""
            hits.append(ChannelHit(
                slug=slug,
                url=f"https://dzen.ru/{slug}",
                title=it.get("title"),
                description=it.get("description"),
                subscribers=parse_count(meta),
                source="search_publisher",
                source_query=query,
            ))
            if len(hits) >= cfg.max_channels_per_query:
                break
        logger.info("[поиск-каналы] '%s': %d каналов", query, len(hits))
    finally:
        await page.close()
    return hits


async def search_channels_from_articles(
    ctx: BrowserContext, query: str, cfg: Config,
) -> list[ChannelHit]:
    """Источник 2: каналы из карточек статей в обычном поиске."""
    url = f"https://dzen.ru/search?query={quote(query)}&type_filter=article%2Cbrief"
    page = await ctx.new_page()
    hits: list[ChannelHit] = []
    try:
        logger.info("[поиск-статьи] %s", query)
        await page.goto(url, wait_until="domcontentloaded")
        await polite_sleep(cfg.min_delay, cfg.max_delay)
        if await _detect_captcha(page):
            logger.warning("[поиск-статьи] '%s': КАПЧА", query)
            return hits
        try:
            await page.wait_for_selector(ARTICLE_CARD, timeout=12000)
        except Exception:
            return hits
        await scroll_until_idle(
            page,
            max_scrolls=cfg.max_scrolls_search,
            idle_rounds=cfg.scroll_idle_rounds,
            item_selector=ARTICLE_CARD,
            min_delay=cfg.min_delay,
            max_delay=cfg.max_delay,
        )
        raw = await page.eval_on_selector_all(ARTICLE_CARD, JS_ARTICLE_CHANNELS)
        seen: set[str] = set()
        for it in raw:
            link = it.get("channel_link")
            slug = channel_slug_from_url(link) if link else None
            if not slug or slug in seen:
                continue
            seen.add(slug)
            hits.append(ChannelHit(
                slug=slug,
                url=f"https://dzen.ru/{slug}",
                title=it.get("channel_title"),
                description=None,
                subscribers=None,
                source="search_article",
                source_query=query,
            ))
        logger.info("[поиск-статьи] '%s': %d каналов", query, len(hits))
    finally:
        await page.close()
    return hits
