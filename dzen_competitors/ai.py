"""AI-функции через OpenRouter (Claude Haiku 4.5).

Два метода:
- expand_niche(niche, description) → список тематических поисковых запросов
- classify_channels(channels, description) → оценки релевантности

Лимит расходов: каждый вызов считает потраченные доллары; если бюджет исчерпан,
возвращает фолбэк (пустой список или None) и пишет в лог.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-haiku-4.5"


@dataclass
class AIResult:
    text: str
    cost: float


class AIBudgetExceeded(Exception):
    pass


class AIClient:
    """Клиент с лимитом расходов на одну сессию (один прогон)."""

    def __init__(self, api_key: str, *, budget_usd: float = 1.0, model: Optional[str] = None):
        if not api_key or not api_key.startswith("sk-or-"):
            raise ValueError("Невалидный ключ OpenRouter (ожидается sk-or-...).")
        self.api_key = api_key
        self.budget = budget_usd
        self.spent = 0.0
        self.model = (model or DEFAULT_MODEL).strip()

    @property
    def remaining(self) -> float:
        return max(self.budget - self.spent, 0.0)

    async def _call(self, system: str, user: str, *, max_tokens: int = 2000) -> AIResult:
        if self.remaining <= 0:
            raise AIBudgetExceeded(f"Бюджет AI исчерпан: ${self.spent:.4f} >= ${self.budget}")
        async with httpx.AsyncClient(timeout=60) as cli:
            r = await cli.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
        if r.status_code != 200:
            raise RuntimeError(f"OpenRouter вернул HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        usage = data.get("usage", {})
        in_tok = usage.get("prompt_tokens", 0)
        out_tok = usage.get("completion_tokens", 0)
        # OpenRouter возвращает точную стоимость в usage.cost
        cost_local = float(usage.get("cost") or 0)
        self.spent += cost_local
        text = data["choices"][0]["message"]["content"]
        return AIResult(text=text, cost=cost_local)

    @staticmethod
    def _extract_json(text: str):
        """Достаёт JSON-массив или объект из текста, даже если он внутри ```json ... ```."""
        m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
        if m:
            text = m.group(1)
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # попробуем найти первый { или [
            for opener, closer in (("[", "]"), ("{", "}")):
                start = text.find(opener)
                end = text.rfind(closer)
                if 0 <= start < end:
                    try:
                        return json.loads(text[start:end + 1])
                    except json.JSONDecodeError:
                        continue
            raise

    # ---------- Публичные методы ----------

    async def expand_niche(self, niche: str, description: str = "", *, n: int = 25) -> list[str]:
        """По нише + описанию возвращает n поисковых запросов для Дзена."""
        system = (
            "Ты помогаешь искать конкурентов в Яндекс.Дзене. "
            "На вход — ниша и опционально описание. "
            "Сгенерируй разнообразные русскоязычные поисковые запросы, по которым в Дзене "
            "найдутся каналы и статьи именно по этой нише. Запросы должны покрывать разные "
            "подтемы и углы зрения. Без воды, без объяснений. Только список JSON-массивом."
        )
        user = f"Ниша: {niche}\n"
        if description.strip():
            user += f"Описание: {description.strip()}\n"
        user += f"Сгенерируй {n} запросов в формате JSON-массива строк. Без markdown-обёрток."
        try:
            res = await self._call(system, user, max_tokens=1200)
        except AIBudgetExceeded:
            logger.warning("AI budget исчерпан до expand_niche")
            return []
        try:
            arr = self._extract_json(res.text)
        except (json.JSONDecodeError, ValueError):
            logger.warning("expand_niche: не удалось распарсить ответ AI: %s", res.text[:200])
            return []
        out: list[str] = []
        seen: set[str] = set()
        for x in arr:
            if not isinstance(x, str):
                continue
            s = x.strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        logger.info("AI expand_niche: %d запросов, $%.4f", len(out), res.cost)
        return out

    async def classify_channels(
        self,
        channels: list[dict],
        niche: str,
        description: str,
        *,
        batch_size: int = 30,
    ) -> dict[str, dict]:
        """channels: список {slug, title, description, top_titles}.
        Возвращает dict slug → {relevance: 0..10, reason: str, category: str}."""
        results: dict[str, dict] = {}
        if not channels:
            return results

        system = (
            "Ты эксперт по контенту. Тебе дают нишу, описание ниши и список каналов "
            "(название, описание, топовые заголовки). Для КАЖДОГО канала оцени релевантность "
            "нише по шкале 0–10 и выдай категорию: профильный | смежный | нерелевантный.\n"
            "Профильный — канал про эту нишу как основную тему.\n"
            "Смежный — частично затрагивает, но основная тема другая.\n"
            "Нерелевантный — не про эту нишу.\n"
            "Учитывай конкретику описания пользователя: если он не хочет лайфстайл-каналы, "
            "снижай оценку и помечай как нерелевантный, даже если ключевые слова есть."
        )

        for i in range(0, len(channels), batch_size):
            if self.remaining <= 0:
                logger.warning("AI budget исчерпан, останавливаю classify на %d/%d", i, len(channels))
                break
            batch = channels[i:i + batch_size]
            payload_lines = []
            for c in batch:
                payload_lines.append({
                    "slug": c["slug"],
                    "title": c.get("title") or "",
                    "description": (c.get("description") or "")[:400],
                    "top_titles": (c.get("top_titles") or [])[:5],
                })
            user = (
                f"Ниша: {niche}\n"
                f"Описание ниши пользователя: {description.strip() or '(не указано)'}\n\n"
                f"Каналы:\n{json.dumps(payload_lines, ensure_ascii=False)}\n\n"
                "Ответь JSON-массивом объектов: "
                '[{"slug":"...","relevance":0-10,"category":"профильный|смежный|нерелевантный","reason":"короткая причина"}]. '
                "Только JSON, без markdown."
            )
            try:
                res = await self._call(system, user, max_tokens=4000)
            except AIBudgetExceeded:
                logger.warning("AI budget исчерпан в середине classify")
                break
            except Exception as e:
                logger.warning("classify_channels: вызов упал: %s", e)
                continue
            try:
                arr = self._extract_json(res.text)
            except (json.JSONDecodeError, ValueError):
                logger.warning("classify: невалидный JSON, пропускаем батч %d", i // batch_size)
                continue
            if not isinstance(arr, list):
                continue
            for item in arr:
                if not isinstance(item, dict):
                    continue
                slug = (item.get("slug") or "").strip()
                if not slug:
                    continue
                results[slug] = {
                    "relevance": _clamp_int(item.get("relevance"), 0, 10),
                    "category": str(item.get("category") or "")[:30],
                    "reason": str(item.get("reason") or "")[:300],
                }
            logger.info("AI classify батч %d: %d/%d, потрачено $%.4f, остаток $%.4f",
                        i // batch_size + 1, len(arr), len(batch), res.cost, self.remaining)
        return results


def _clamp_int(v, lo, hi):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, n))


def mask_key(key: str) -> str:
    """Возвращает маску ключа для логов: первые 8 + ... + последние 4 символа."""
    if not key or len(key) < 16:
        return "***"
    return f"{key[:8]}...{key[-4:]}"
