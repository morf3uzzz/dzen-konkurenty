"""AI-функции через OpenRouter (Claude Haiku 4.5).

Два метода:
- expand_niche(niche, description) → список тематических поисковых запросов
- classify_channels(channels, description) → оценки релевантности

Лимит расходов: каждый вызов считает потраченные доллары; если бюджет исчерпан,
возвращает фолбэк (пустой список или None) и пишет в лог.
"""
from __future__ import annotations

import asyncio
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

    async def _call(self, system: str, user: str, *, max_tokens: int = 2000,
                    retries: int = 3) -> AIResult:
        if self.remaining <= 0:
            raise AIBudgetExceeded(f"Бюджет AI исчерпан: ${self.spent:.4f} >= ${self.budget}")

        # GPT-5/o-series тратят токены на reasoning. Если max_tokens слишком мал,
        # content вернётся пустым. Для них утраиваем лимит.
        is_reasoning = (
            self.model.startswith("openai/gpt-5") or
            self.model.startswith("openai/o") or
            "reasoning" in self.model
        )
        effective_max = max_tokens * 3 if is_reasoning else max_tokens

        last_err: Optional[str] = None
        for attempt in range(retries):
            try:
                async with httpx.AsyncClient(timeout=120) as cli:
                    r = await cli.post(
                        OPENROUTER_URL,
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": self.model,
                            "max_tokens": effective_max,
                            "messages": [
                                {"role": "system", "content": system},
                                {"role": "user", "content": user},
                            ],
                        },
                    )
                if r.status_code in (401, 403):
                    # Сразу падаем — повторы бессмысленны.
                    raise RuntimeError(
                        f"OpenRouter отверг ключ (HTTP {r.status_code}). "
                        f"Проверь его на openrouter.ai/keys или пополни баланс."
                    )
                if r.status_code in (429, 500, 502, 503, 504):
                    last_err = f"HTTP {r.status_code}"
                    if attempt < retries - 1:
                        wait = 2 ** attempt
                        logger.warning("OpenRouter %d, ждём %ds", r.status_code, wait)
                        await asyncio.sleep(wait)
                        continue
                    raise RuntimeError(f"OpenRouter недоступен после {retries} попыток: {last_err}")
                if r.status_code != 200:
                    raise RuntimeError(f"OpenRouter HTTP {r.status_code}: {r.text[:200]}")
                try:
                    data = r.json()
                except (ValueError, json.JSONDecodeError) as e:
                    raise RuntimeError(f"OpenRouter не-JSON ответ: {e}") from e
                usage = data.get("usage", {})
                cost_local = float(usage.get("cost") or 0)
                self.spent += cost_local
                choices = data.get("choices") or []
                if not choices:
                    raise RuntimeError("OpenRouter вернул пустой choices")
                msg = choices[0].get("message") or {}
                text = msg.get("content") or ""
                # Reasoning-модели иногда отдают только reasoning, без content (если max_tokens мал).
                if not text:
                    text = msg.get("reasoning") or msg.get("reasoning_content") or ""
                if not text:
                    raise RuntimeError(f"Пустой ответ от {self.model} (увеличь max_tokens?)")
                return AIResult(text=text, cost=cost_local)
            except (httpx.HTTPError, asyncio.TimeoutError) as e:
                last_err = repr(e)
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"OpenRouter сеть недоступна: {last_err}")

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
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        current_year = datetime.now().year
        system = (
            "Ты — стратег по контент-маркетингу, который помогает находить конкурентов "
            "в Яндекс.Дзене для конкретного бизнеса. Твоя задача — сгенерировать "
            f"{n} разнообразных поисковых запросов, по которым в Дзене найдутся "
            "именно те каналы, которые являются настоящими конкурентами этого бизнеса.\n\n"

            "ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА:\n"
            "1. ЯЗЫК. Только русский. Никаких англицизмов и калек («флипинг», «сторителлинг», "
            "«нетворкинг», «инсайдер», «маркетплейс»). Если такое слово используют сами носители "
            "ниши — допустимо, но проверь себя на здравый смысл.\n"
            "2. ЕСТЕСТВЕННОСТЬ. Запросы должны выглядеть так, как реальный человек печатает в "
            "поисковой строке Яндекса. Короткие фразы 2–5 слов лучше длинных «риск-менеджмент в "
            "недвижимости». Не используй академические/корпоративные обороты.\n"
            f"3. ГОД. Если ставишь год в запрос — только {current_year} (текущий). Чаще всего "
            "год не нужен совсем.\n"
            "4. ТОЧНОСТЬ ПОПАДАНИЯ. Каждый запрос должен с большой вероятностью вернуть "
            "именно конкурентов этого бизнеса, а не аудиторию его клиентов и не другую "
            "тему. Если бизнес «бренд женской одежды на маркетплейсе» — конкуренты это другие "
            "бренды и retail-эксперты, а НЕ fashion-блогеры для покупательниц. Если бизнес "
            "«частный психиатр с медобразованием» — конкуренты это другие врачи-психотерапевты "
            "(не коучи и не тарологи). Думай: «Если человек ищет ИМЕННО таких как этот бизнес, "
            "что он печатает?»\n"
            "5. ГЕОГРАФИЯ. Если в описании указан город или регион — добавляй его в "
            "часть запросов («юрист по ЖКХ Москва», «риэлтор Краснодар»). Не добавляй в каждый — "
            "только там, где это реально сужает поиск.\n"
            "6. УГОЛ ЗРЕНИЯ. Покрывай разные подтемы: услуги, обучение, обзоры, проблемы клиентов, "
            "сравнения, юридические/налоговые/финансовые аспекты, отраслевая аналитика. Не делай "
            "25 однотипных вариаций одной фразы.\n"
            "7. БЕЗ КЛИКБЕЙТА И ЭЗОТЕРИКИ. Не генерируй «секреты», «как заработать миллион», "
            "«мистическая правда». Это привлечёт мусорные каналы.\n"
            "8. ИЗБЕГАЙ ОБЩИХ СЛОВ. Запрос «инвестиции» или «бизнес» вернёт огромный пул общих "
            "блогеров, среди которых конкурентов почти не будет. Уточняй: «инвестиции в "
            "коммерческую недвижимость», «бизнес в строительстве».\n\n"

            "ФОРМАТ ОТВЕТА: только JSON-массив строк, без markdown-обёрток, без объяснений."
        )
        user = (
            f"Сегодняшняя дата: {today}\n"
            f"Ниша / бизнес: {niche}\n"
        )
        if description.strip():
            user += f"Контекст бизнеса от пользователя: {description.strip()}\n"
        else:
            user += (
                "Контекст не указан. Будь особенно осторожен: без контекста легко "
                "сгенерировать слишком общие запросы. Делай конкретно по слову ниши.\n"
            )
        user += (
            f"\nСгенерируй ровно {n} поисковых запросов. "
            "Каждый запрос — короткая русская фраза 2–5 слов. "
            "Только JSON-массив строк."
        )
        try:
            res = await self._call(system, user, max_tokens=1200)
        except AIBudgetExceeded:
            logger.warning("AI budget исчерпан до expand_niche")
            return []
        except RuntimeError as e:
            logger.error("AI ошибка: %s", e)
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
            "Ты — стратег конкурентного анализа. Тебе дают конкретный бизнес "
            "(нишу + описание) и список Дзен-каналов с их названиями, описаниями и топовыми "
            "заголовками. Для КАЖДОГО канала строго оцени, насколько он реально является "
            "конкурентом ИМЕННО этого бизнеса.\n\n"

            "ШКАЛА: relevance 0–10.\n"
            "  9–10 — прямой конкурент: тот же продукт/услуга для той же аудитории.\n"
            "  6–8  — близкий по тематике, но другой угол / сегмент / уровень.\n"
            "  3–5  — смежная тема, может быть для коллабораций, но не конкурент.\n"
            "  1–2  — формально пересекаются ключевые слова, но по сути не наш кейс.\n"
            "  0    — не имеет отношения к нише.\n\n"

            "КАТЕГОРИИ (ровно одна):\n"
            "  «прямой конкурент» — продаёт/делает похожее на тот же сегмент аудитории.\n"
            "  «смежный» — близкая отрасль или другой угол этой же ниши.\n"
            "  «аудитория» — пишет ДЛЯ потенциальных клиентов нашего бизнеса, а не конкурирует.\n"
            "  «нерелевантный» — не про эту нишу совсем.\n\n"

            "ЖЁСТКИЕ ПРАВИЛА:\n"
            "1. Различай «контент про X» и «бизнес продающий X». Канал «как одеваться "
            "стильно» — это аудитория для бренда одежды, а не конкурент бренда.\n"
            "2. Если бизнес узкоспециализированный (медицинский психиатр, элитная юр-практика, "
            "B2B-сервис) — общие каналы по теме ставь не выше 4. Прямой конкурент — это "
            "канал того же уровня экспертизы и сегмента рынка.\n"
            "3. Учитывай ЯВНЫЕ исключения из описания пользователя. Если он написал «не хочу "
            "лайфстайл», «не интересуют мам в декрете», «без эзотериков» — такие каналы получают "
            "0–2 и категорию «нерелевантный», даже если ключевые слова есть.\n"
            "4. Если в топовых заголовках видно кликбейт («вы не поверите», «секрет миллионеров», "
            "«их жизнь после»), а в названии что-то экспертное — суди по реальному контенту, "
            "то есть по заголовкам. Кликбейт-канал → не выше 3.\n"
            "5. Если описание канала пустое и заголовков мало — будь консервативен. Лучше "
            "поставить ниже, чем выше: пользователь сам пересмотрит, если ошиблись в минус.\n"
            "6. «reason» — одно короткое предложение: что именно совпадает / не совпадает с нишей. "
            "Не лей воду. Не пересказывай заголовки.\n"
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
                f"Ниша / бизнес: {niche}\n"
                f"Описание от пользователя: {description.strip() or '(не указано)'}\n\n"
                f"Каналы для оценки:\n{json.dumps(payload_lines, ensure_ascii=False)}\n\n"
                "Ответь JSON-массивом объектов вида:\n"
                '[{"slug":"...","relevance":0-10,'
                '"category":"прямой конкурент|смежный|аудитория|нерелевантный",'
                '"reason":"одно короткое предложение"}]\n'
                "Только JSON, без markdown-обёрток, без преамбулы."
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
