"""Генерация CSV-отчётов: каналы и статьи."""
from __future__ import annotations

import csv
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from storage import Storage


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name).strip("_")[:60] or "run"


def _ts_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _human_date(ts: Optional[int]) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


def _days_ago(ts: Optional[int]) -> Optional[int]:
    if not ts:
        return None
    now = datetime.now(timezone.utc).timestamp()
    return max(0, int((now - ts) / 86400))


def write_channels_csv(
    storage: Storage, run_id: int, niche: str, out_dir: Path,
    *, min_subs: int = 0,
) -> Path:
    """1 строка = 1 канал. Сортировка: релевантность DESC, потом скор.
    В CSV попадают только каналы, у которых subscribers >= min_subs."""
    slugs = storage.channel_slugs_for_run(run_id)
    channels = {r["slug"]: r for r in storage.channels_by_slugs(slugs)}
    articles = storage.articles_for_channels(slugs, run_id)
    hits = storage.hit_counts_for_run(run_id)

    by_slug: dict[str, list] = {}
    for a in articles:
        by_slug.setdefault(a["channel_slug"], []).append(a)

    rows: list[dict] = []
    for slug in slugs:
        ch = channels.get(slug)
        if not ch:
            continue
        # Фильтр по подписчикам — пользователь явно указал минимум.
        if min_subs > 0 and (ch["subscribers"] or 0) < min_subs:
            continue
        arts = by_slug.get(slug, [])
        views = [a["views"] for a in arts if a["views"] is not None]
        ttr = [a["views_till_end"] for a in arts if a["views_till_end"] is not None]
        # дочитываемость = sum(viewsTillEnd) / sum(views) — точнее, чем по статье
        # views_till_end должен быть <= views (на сломанных записях бывает иначе — клампим)
        view_total = sum(views) if views else 0
        ttr_total = 0
        for a in arts:
            v = a["views"] or 0
            t = a["views_till_end"] or 0
            ttr_total += min(t, v)
        read_through = (ttr_total / view_total) if view_total else None

        # частота за 30 дней
        ts_30d = (datetime.now(timezone.utc).timestamp() - 30 * 86400)
        in_30d = [a for a in arts if a["publication_ts"] and a["publication_ts"] >= ts_30d]
        per_week = (len(in_30d) / 30 * 7) if in_30d else None

        # Скор
        subs = ch["subscribers"] or 0
        rel = ch["relevance"] or 0
        score = (
            math.log10(max(subs, 1) + 1) * 1.0
            + math.log10(max(hits.get(slug, 0), 0) + 1) * 1.5
            + math.log10(max(int((sum(views) / len(views)) if views else 0), 1) + 1) * 1.0
            + (read_through or 0) * 2.0
            + (rel / 10) * 2.0
        )

        # Топ-3 статей по просмотрам
        top_arts = sorted(arts, key=lambda a: -(a["views"] or 0))[:3]

        def _top_field(idx: int, key: str):
            return top_arts[idx][key] if len(top_arts) > idx else None

        def _top_ttr(idx: int):
            if len(top_arts) <= idx:
                return None
            a = top_arts[idx]
            v = a["views"] or 0
            t = a["views_till_end"] or 0
            if not v:
                return None
            return min(t, v) / v

        rows.append({
            "slug": slug,
            "title": ch["title"] or "",
            "url": ch["url"],
            "description": ch["description"] or "",
            "subscribers": subs,
            "relevance": ch["relevance"],
            "category": ch["category"] or "",
            "reason": ch["relevance_reason"] or "",
            "niche_hits": hits.get(slug, 0),
            "articles_total": len(arts),
            "articles_30d": len(in_30d),
            "posts_per_week_30d": per_week,
            "median_views": int(sorted(views)[len(views)//2]) if views else None,
            "max_views": max(views) if views else None,
            "read_through_rate": read_through,
            "score": round(score, 3),
            "top1_title": _top_field(0, "title") or "",
            "top1_url": _top_field(0, "url") or "",
            "top1_views": _top_field(0, "views"),
            "top1_ttr": _top_ttr(0),
            "top2_title": _top_field(1, "title") or "",
            "top2_url": _top_field(1, "url") or "",
            "top2_views": _top_field(1, "views"),
            "top2_ttr": _top_ttr(1),
            "top3_title": _top_field(2, "title") or "",
            "top3_url": _top_field(2, "url") or "",
            "top3_views": _top_field(2, "views"),
            "top3_ttr": _top_ttr(2),
        })

    rows.sort(key=lambda r: (-(r["relevance"] or 0), -r["score"]))

    path = out_dir / f"каналы_{_safe(niche)}_{_ts_str()}.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "Ранг", "Релевантность 0-10", "Категория", "Причина",
            "Канал (slug)", "Название канала", "Ссылка на канал", "Описание",
            "Подписчики", "Скор",
            "Попаданий в нишу", "Собрано статей", "Статей за 30 дней",
            "Постов/неделю за 30 дней",
            "Медиана просмотров", "Макс просмотров",
            "Дочитываемость, %",
            "Топ-1 заголовок", "Топ-1 просмотры", "Топ-1 дочитываемость, %", "Топ-1 ссылка",
            "Топ-2 заголовок", "Топ-2 просмотры", "Топ-2 дочитываемость, %", "Топ-2 ссылка",
            "Топ-3 заголовок", "Топ-3 просмотры", "Топ-3 дочитываемость, %", "Топ-3 ссылка",
        ])

        def _ttr_pct(v):
            if v is None:
                return ""
            return f"{round(v * 100, 1)}%"

        for i, r in enumerate(rows, 1):
            w.writerow([
                i, r["relevance"] if r["relevance"] is not None else "",
                r["category"], (r["reason"] or "")[:300],
                r["slug"], r["title"], r["url"], (r["description"] or "")[:600],
                r["subscribers"] or "", r["score"],
                r["niche_hits"], r["articles_total"], r["articles_30d"],
                round(r["posts_per_week_30d"], 2) if r["posts_per_week_30d"] is not None else "",
                r["median_views"] or "", r["max_views"] or "",
                _ttr_pct(r["read_through_rate"]),
                r["top1_title"], r["top1_views"] or "", _ttr_pct(r["top1_ttr"]), r["top1_url"],
                r["top2_title"], r["top2_views"] or "", _ttr_pct(r["top2_ttr"]), r["top2_url"],
                r["top3_title"], r["top3_views"] or "", _ttr_pct(r["top3_ttr"]), r["top3_url"],
            ])
    return path


def write_articles_csv(
    storage: Storage, run_id: int, niche: str, out_dir: Path,
    *, min_subs: int = 0,
) -> Path:
    """1 строка = 1 статья. Сортировка: канал-релевантность DESC, потом просмотры DESC.
    Включаются только статьи каналов, прошедших фильтр по подписчикам."""
    slugs = storage.channel_slugs_for_run(run_id)
    channels = {r["slug"]: r for r in storage.channels_by_slugs(slugs)}
    articles = storage.articles_for_channels(slugs, run_id)

    # Список slug'ов прошедших фильтр.
    if min_subs > 0:
        allowed = {s for s, ch in channels.items() if (ch["subscribers"] or 0) >= min_subs}
    else:
        allowed = set(channels.keys())

    rows: list[dict] = []
    for a in articles:
        if a["channel_slug"] not in allowed:
            continue
        ch = channels.get(a["channel_slug"]) or {}
        days = _days_ago(a["publication_ts"])
        ttr_rate = None
        if a["views"] and a["views_till_end"] is not None:
            clamped = min(a["views_till_end"], a["views"])
            ttr_rate = clamped / a["views"]
        rows.append({
            "title": a["title"] or "",
            "lead": a["lead"] or "",
            "views": a["views"],
            "views_till_end": a["views_till_end"],
            "ttr_rate": ttr_rate,
            "ttr_sec": a["time_to_read_sec"],
            "pub_date": _human_date(a["publication_ts"]),
            "days_ago": days,
            "in_30d": "Да" if days is not None and days <= 30 else "Нет",
            "channel_slug": a["channel_slug"],
            "channel_title": ch["title"] if ch else "",
            "channel_url": ch["url"] if ch else f"https://dzen.ru/{a['channel_slug']}",
            "channel_subs": ch["subscribers"] if ch else None,
            "channel_relevance": ch["relevance"] if ch else None,
            "url": a["url"],
        })

    rows.sort(key=lambda r: (
        -(r["channel_relevance"] or 0),
        -(r["views"] or 0),
    ))

    path = out_dir / f"статьи_{_safe(niche)}_{_ts_str()}.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "Ранг", "Заголовок", "Лид (первый абзац)",
            "Просмотры", "Дочитывания", "Дочитываемость, %",
            "Время чтения (сек)",
            "Дата публикации", "Дней назад", "В окне 30 дней",
            "Релевантность канала", "Название канала", "Ссылка на канал", "Подписчиков канала",
            "Ссылка на статью",
        ])

        def _ttr_pct(v):
            return f"{round(v * 100, 1)}%" if v is not None else ""

        for i, r in enumerate(rows, 1):
            w.writerow([
                i, r["title"], (r["lead"] or "")[:800],
                r["views"] if r["views"] is not None else "",
                r["views_till_end"] if r["views_till_end"] is not None else "",
                _ttr_pct(r["ttr_rate"]),
                r["ttr_sec"] if r["ttr_sec"] is not None else "",
                r["pub_date"], r["days_ago"] if r["days_ago"] is not None else "",
                r["in_30d"],
                r["channel_relevance"] if r["channel_relevance"] is not None else "",
                r["channel_title"], r["channel_url"], r["channel_subs"] or "",
                r["url"],
            ])
    return path
