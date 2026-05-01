"""HTTP-клиент к публичному JSON-API Дзена.
Большинство данных доступны без аутентификации — это даёт огромное ускорение
по сравнению с DOM-парсингом через Playwright.

Использует httpx + ретраи с exponential backoff на 429 / 5xx.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": "https://dzen.ru/",
}


@dataclass
class ArticleData:
    url: str                         # каноничная ссылка на статью /a/<hash>
    title: Optional[str]
    lead: Optional[str]              # text-поле API — лид
    views: Optional[int]
    views_till_end: Optional[int]    # дочитывания
    time_to_read_sec: Optional[int]
    publication_ts: Optional[int]    # UNIX timestamp
    comments_link: Optional[str]


@dataclass
class ChannelFeedResult:
    articles: list[ArticleData]
    next_link: Optional[str]
    publisher_subscribers: Optional[int]   # подписчики канала из publisher блока


def _canonical_article_url(link: str) -> str:
    """Убираем query-параметры — оставляем только /a/<hash>."""
    if not link:
        return link
    if "?" in link:
        link = link.split("?", 1)[0]
    if "#" in link:
        link = link.split("#", 1)[0]
    return link


def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class DzenAPI:
    """Async-клиент. Используется в `async with`."""

    def __init__(self, *, max_concurrency: int = 6, request_timeout: int = 20):
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._client: Optional[httpx.AsyncClient] = None
        self._timeout = request_timeout

    async def __aenter__(self) -> "DzenAPI":
        self._client = httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            timeout=self._timeout,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get_json(self, url: str, *, retries: int = 3) -> Optional[dict]:
        assert self._client is not None
        last_err: Optional[Exception] = None
        async with self._semaphore:
            for attempt in range(retries):
                try:
                    r = await self._client.get(url)
                    if r.status_code == 200:
                        return r.json()
                    if r.status_code in (429, 500, 502, 503, 504):
                        wait = 2 ** attempt
                        logger.warning("API %d: %s, ждём %ds", r.status_code, url[:80], wait)
                        await asyncio.sleep(wait)
                        continue
                    if r.status_code == 404:
                        return None
                    logger.warning("API %d: %s", r.status_code, url[:120])
                    return None
                except (httpx.HTTPError, asyncio.TimeoutError) as e:
                    last_err = e
                    await asyncio.sleep(2 ** attempt)
            logger.warning("API failed после %d попыток: %s — %s", retries, url[:120], last_err)
            return None

    # ---------- channel-more: лента канала ----------

    async def fetch_channel_feed(
        self,
        slug: str,
        *,
        sort: str = "regular",      # regular | popular ... regular = «сначала новое»
        max_pages: int = 5,
        tab: str = "articles",
    ) -> ChannelFeedResult:
        """Загружает ленту канала через `channel-more` API.

        Возвращает все статьи + ссылку на следующую страницу (если осталась).
        Сейчас только sort=regular надёжно работает; popular API возвращает 404.
        """
        all_articles: list[ArticleData] = []
        publisher_subs: Optional[int] = None
        url = (
            f"https://dzen.ru/api/web/v1/channel-more"
            f"?channel_name={quote(slug, safe='/')}"
            f"&sort_type={sort}&country_code=ru&tab={tab}"
        )
        seen_urls: set[str] = set()
        for page_idx in range(max_pages):
            data = await self._get_json(url)
            if not data:
                break
            items = data.get("items") or []
            for it in items:
                if it.get("type") != "article":
                    continue
                link_raw = it.get("link") or it.get("shareLink") or ""
                canon = _canonical_article_url(link_raw)
                if not canon or canon in seen_urls:
                    continue
                seen_urls.add(canon)
                all_articles.append(ArticleData(
                    url=canon,
                    title=it.get("title"),
                    lead=it.get("text"),
                    views=_to_int(it.get("views")),
                    views_till_end=_to_int(it.get("viewsTillEnd")),
                    time_to_read_sec=_to_int(it.get("timeToReadSeconds")),
                    publication_ts=_to_int(it.get("publicationDate")),
                    comments_link=it.get("allCommentsLink"),
                ))
                # Подписчики у publisher
                pub = it.get("publisher") or {}
                if publisher_subs is None and pub:
                    publisher_subs = _to_int(pub.get("subscribers"))

            more = data.get("more") or {}
            next_link = more.get("link")
            if not next_link:
                return ChannelFeedResult(all_articles, None, publisher_subs)
            url = next_link

        return ChannelFeedResult(all_articles, url, publisher_subs)

    # ---------- recommend похожих каналов ----------

    async def fetch_similar_channels(self, slug: str) -> list[dict]:
        """Возвращает список похожих/рекомендуемых каналов.
        Эндпоинт `recommend-topic-channels-heads` отдаёт похожие каналы темы.
        """
        url = (
            f"https://dzen.ru/api/web/v1/recommend-topic-channels-heads"
            f"?clid=1400&channel_name={quote(slug, safe='/')}"
        )
        data = await self._get_json(url)
        if not data:
            return []
        return data.get("topicChannelHeads") or []
