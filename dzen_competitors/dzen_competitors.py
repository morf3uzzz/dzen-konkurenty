"""Главный оркестратор глубокого парсера каналов Дзена.

Стадии:
  1. AI: расширение ниши в N тематических запросов.
  2. Поиск каналов: вкладка "Каналы" (Playwright) + каналы из карточек статей.
  3. Отсев: только каналы с подписчиками >= MIN_SUBS.
  4. Детальный анализ: лента канала через JSON-API (httpx).
  5. AI-классификация: релевантность + категория + причина.
  6. CSV: каналы + статьи.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from api import DzenAPI
from ai import AIClient, mask_key
from browser import browser_context, polite_sleep
from config import Config
from reporter import write_channels_csv, write_articles_csv
from search_parser import (
    ChannelHit,
    search_channels_publisher,
    search_channels_from_articles,
)
from storage import Storage


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("dzen_competitors")


@dataclass
class RunParams:
    niche: str
    description: str
    min_subs: int
    api_key: Optional[str]
    queries_count: int = 25       # запросов к Дзену


# ---------- Стадии ----------

async def stage_expand_queries(ai: Optional[AIClient], niche: str, description: str, n: int) -> list[str]:
    """Стадия 1: AI генерит запросы. Если AI недоступен — фолбэк на YAML-шаблон."""
    if ai is None:
        from config import load_queries
        ROOT = Path(__file__).parent
        logger.info("[1/5] AI отключён, использую шаблон queries_example.yaml")
        return load_queries(niche, ROOT / "queries_example.yaml")
    logger.info("[1/5] AI расширяет нишу «%s» в %d запросов…", niche, n)
    queries = await ai.expand_niche(niche, description, n=n)
    if not queries:
        logger.warning("[1/5] AI вернул пусто — фолбэк на шаблон")
        from config import load_queries
        return load_queries(niche, Path(__file__).parent / "queries_example.yaml")
    return queries


async def stage_collect_channels(
    storage: Storage, run_id: int, queries: list[str], cfg: Config,
) -> dict[str, ChannelHit]:
    """Стадия 2: два источника — поиск каналов и каналы из статей.
    Возвращает словарь slug → ChannelHit с лучшей информацией."""
    found: dict[str, ChannelHit] = {}
    async with browser_context(cfg.headless, cfg.browser_timeout) as ctx:
        for i, q in enumerate(queries, 1):
            logger.info("[2/5] Запрос %d/%d: «%s»", i, len(queries), q)
            # Источник 1: вкладка "Каналы"
            try:
                hits1 = await search_channels_publisher(ctx, q, cfg)
            except Exception as e:
                logger.warning("[2/5] поиск-каналы упал: %s", e)
                hits1 = []
            # Источник 2: каналы из статей
            try:
                hits2 = await search_channels_from_articles(ctx, q, cfg)
            except Exception as e:
                logger.warning("[2/5] поиск-статьи упал: %s", e)
                hits2 = []
            for hit in hits1 + hits2:
                _merge_hit(found, hit)
                storage.upsert_channel(
                    slug=hit.slug, url=hit.url,
                    title=hit.title, description=hit.description,
                    subscribers=hit.subscribers,
                )
                storage.record_hit(run_id, hit.slug, hit.source, hit.source_query)
            await polite_sleep(cfg.min_delay, cfg.max_delay)
    logger.info("[2/5] Найдено уникальных каналов: %d", len(found))
    return found


def _merge_hit(found: dict[str, ChannelHit], hit: ChannelHit) -> None:
    if hit.slug not in found:
        found[hit.slug] = hit
        return
    cur = found[hit.slug]
    # дозаполняем None-поля
    if cur.title is None and hit.title:
        cur.title = hit.title
    if cur.description is None and hit.description:
        cur.description = hit.description
    if cur.subscribers is None and hit.subscribers is not None:
        cur.subscribers = hit.subscribers


async def stage_detailed_analysis(
    storage: Storage, run_id: int, slugs: list[str], cfg: Config,
) -> int:
    """Стадия 5: для каждого канала тянем ленту через httpx-API параллельно."""
    total_count = len(slugs)
    logger.info("[4/5] Детальный анализ %d каналов через API…", total_count)
    total_articles = 0
    completed = 0

    async with DzenAPI(max_concurrency=cfg.api_concurrency) as api:
        sem = asyncio.Semaphore(cfg.api_concurrency)

        async def one(slug: str) -> int:
            nonlocal completed
            async with sem:
                try:
                    feed = await api.fetch_channel_feed(slug, max_pages=cfg.api_max_pages_per_channel)
                except Exception as e:
                    logger.warning("[4/5] %s упал: %s", slug, e)
                    completed += 1
                    return 0
            for art in feed.articles:
                storage.upsert_article(
                    url=art.url, channel_slug=slug,
                    title=art.title, lead=art.lead,
                    views=art.views, views_till_end=art.views_till_end,
                    time_to_read_sec=art.time_to_read_sec,
                    publication_ts=art.publication_ts,
                    run_id=run_id,
                )
            completed += 1
            # Логируем каждый 5-й канал, чтобы UI имел прогресс
            if completed % 5 == 0 or completed == total_count:
                logger.info("[4/5] прогресс: %d/%d каналов", completed, total_count)
            return len(feed.articles)

        results = await asyncio.gather(*[one(s) for s in slugs])
        total_articles = sum(results)
    logger.info("[4/5] Собрано статей: %d", total_articles)
    return total_articles


async def stage_classify(
    ai: AIClient, storage: Storage, slugs: list[str], niche: str, description: str,
) -> int:
    """Стадия 6: AI оценивает релевантность каждого канала."""
    rows = storage.channels_by_slugs(slugs)
    payload: list[dict] = []
    for r in rows:
        # топовые заголовки для контекста
        with storage._conn() as c:
            arts = c.execute(
                "SELECT title FROM articles WHERE channel_slug=? AND title IS NOT NULL "
                "ORDER BY COALESCE(views, 0) DESC LIMIT 5",
                (r["slug"],),
            ).fetchall()
        payload.append({
            "slug": r["slug"],
            "title": r["title"] or "",
            "description": r["description"] or "",
            "top_titles": [a["title"] for a in arts],
        })
    logger.info("[5/5] AI классифицирует %d каналов…", len(payload))
    try:
        result = await ai.classify_channels(payload, niche, description)
    except Exception as e:
        logger.warning("[5/5] classify упал: %s — пропускаю", e)
        return 0
    for slug, info in result.items():
        storage.update_channel_classification(
            slug,
            relevance=info.get("relevance"),
            category=info.get("category"),
            reason=info.get("reason"),
        )
    return len(result)


# ---------- Главный run ----------

async def run(params: RunParams, cfg: Config) -> None:
    cfg.ensure_dirs()
    storage = Storage(cfg.db_path)
    run_id = storage.start_run(params.niche, params.description)
    logger.info("=== Прогон %d: «%s» (мин. подписчиков: %d) ===",
                run_id, params.niche, params.min_subs)
    if params.api_key:
        logger.info("AI: ключ OpenRouter %s", mask_key(params.api_key))

    ai: Optional[AIClient] = None
    if params.api_key:
        try:
            model = os.getenv("DZEN_OPENROUTER_MODEL", "").strip() or None
            ai = AIClient(params.api_key, budget_usd=cfg.ai_budget_usd, model=model)
            if model:
                logger.info("AI модель: %s", model)
        except ValueError as e:
            logger.warning("AI отключён: %s", e)

    finished = False
    try:
        # Стадия 1: Расширение запросов через AI
        queries = await stage_expand_queries(
            ai, params.niche, params.description, params.queries_count,
        )
        logger.info("[1/5] Запросов готово: %d", len(queries))

        # Стадия 2: Сбор каналов из 2 источников поиска
        found = await stage_collect_channels(storage, run_id, queries, cfg)

        # Стадия 3: Отсев по подписчикам
        all_slugs = list(found.keys())
        all_rows = {r["slug"]: r for r in storage.channels_by_slugs(all_slugs)}
        passed = [s for s in all_slugs
                  if (all_rows.get(s) and (all_rows[s]["subscribers"] or 0) >= params.min_subs)]
        logger.info("[3/5] Прошли фильтр (>= %d подписчиков): %d из %d",
                    params.min_subs, len(passed), len(all_slugs))

        # Стадия 4: Детальный анализ — статьи для прошедших фильтр
        articles_count = await stage_detailed_analysis(storage, run_id, passed, cfg)

        # Стадия 5: AI-классификация
        classified = 0
        if ai is not None and ai.remaining > 0:
            classified = await stage_classify(
                ai, storage, passed, params.niche, params.description,
            )
        else:
            logger.info("[5/5] Пропущено (нет AI или бюджет 0)")

        storage.finish_run(run_id, queries=len(queries),
                           channels=len(passed), articles=articles_count)
        finished = True

        # Отчёты — фильтруем по тому же min_subs, что использовали для анализа
        ch_csv = write_channels_csv(
            storage, run_id, params.niche, cfg.report_dir,
            min_subs=params.min_subs,
        )
        art_csv = write_articles_csv(
            storage, run_id, params.niche, cfg.report_dir,
            min_subs=params.min_subs,
        )
        logger.info("=== Готово. Каналов %d, статей %d, классифицировано %d ===",
                    len(passed), articles_count, classified)
        logger.info("CSV каналов: %s", ch_csv)
        logger.info("CSV статей: %s", art_csv)
        if ai is not None:
            logger.info("AI потрачено: $%.4f", ai.spent)
    except KeyboardInterrupt:
        logger.warning("Прерывание пользователем")
        # Если уже сделали finish_run — не перезаписывать. Иначе сохраняем то, что успели.
        if not locals().get("finished"):
            try:
                partial_slugs = storage.channel_slugs_for_run(run_id)
                partial_arts = storage.articles_for_channels(partial_slugs, run_id)
                storage.finish_run(run_id,
                                   queries=0,
                                   channels=len(partial_slugs),
                                   articles=len(partial_arts))
            except Exception:
                pass
        raise


# ---------- CLI ----------

def main() -> None:
    p = argparse.ArgumentParser(prog="dzen_competitors")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="Глубокий прогон по нише")
    r.add_argument("--niche", required=True, help="Название ниши")
    r.add_argument("--description", default="", help="Подробное описание ниши для AI")
    r.add_argument("--min-subs", type=int, default=1000, help="Мин. подписчиков для детального анализа")
    r.add_argument("--output-dir", default="", help="Куда складывать CSV (по умолчанию data/)")
    args = p.parse_args()

    cfg = Config()
    if args.output_dir:
        cfg.report_dir = Path(args.output_dir)
    api_key = os.getenv("DZEN_OPENROUTER_KEY", "").strip() or None
    if args.cmd == "run":
        params = RunParams(
            niche=args.niche.strip(),
            description=args.description.strip(),
            min_subs=args.min_subs,
            api_key=api_key,
            queries_count=25,
        )
        try:
            asyncio.run(run(params, cfg))
        except KeyboardInterrupt:
            sys.exit(130)
    else:
        p.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
