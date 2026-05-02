import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
# override=False: реальная переменная окружения (от родительского процесса) важнее .env-файла
load_dotenv(ROOT / ".env", override=False)


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    return float(raw) if raw else default


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    return int(raw) if raw else default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    # Браузер для поиска каналов (Playwright). По умолчанию скрыт — приложение
    # не должно мигать окнами Chromium для пользователя.
    headless: bool = _env_bool("HEADLESS", True)
    browser_timeout: int = _env_int("BROWSER_TIMEOUT", 60000)

    min_delay: float = _env_float("MIN_DELAY", 1.0)
    max_delay: float = _env_float("MAX_DELAY", 2.5)

    max_scrolls_search: int = _env_int("MAX_SCROLLS_SEARCH", 12)
    scroll_idle_rounds: int = _env_int("SCROLL_IDLE_ROUNDS", 3)

    max_channels_per_query: int = _env_int("MAX_CHANNELS_PER_QUERY", 30)

    # API-клиент Дзена (httpx)
    api_concurrency: int = _env_int("API_CONCURRENCY", 6)
    api_max_pages_per_channel: int = _env_int("API_MAX_PAGES_PER_CHANNEL", 5)

    # AI (OpenRouter)
    ai_budget_usd: float = _env_float("AI_BUDGET_USD", 1.0)

    db_path: Path = ROOT / os.getenv("DB_PATH", "data/dzen_competitors.sqlite")
    report_dir: Path = ROOT / os.getenv("REPORT_DIR", "data")

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)


def load_queries(niche: str, queries_file: Path) -> List[str]:
    """Фолбэк-генератор запросов по YAML-шаблону, если AI недоступен."""
    with open(queries_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    modifiers = data.get("modifiers") or [""]
    extra = data.get("extra") or []
    niche = niche.strip()
    queries: List[str] = []
    for mod in modifiers:
        mod = (mod or "").strip()
        q = f"{niche} {mod}".strip() if mod else niche
        queries.append(q)
    queries.extend([str(x).strip() for x in extra if str(x).strip()])
    seen, out = set(), []
    for x in queries:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out
