import asyncio
import logging
import random
from contextlib import asynccontextmanager

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@asynccontextmanager
async def browser_context(headless: bool, timeout_ms: int):
    """Одноразовый анонимный контекст Chromium. storage_state не сохраняется — чистый профиль."""
    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context: BrowserContext = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=USER_AGENT,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
        )
        context.set_default_timeout(timeout_ms)
        try:
            yield context
        finally:
            await context.close()
            await browser.close()


async def polite_sleep(min_s: float, max_s: float) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


async def scroll_until_idle(
    page: Page,
    max_scrolls: int,
    idle_rounds: int,
    item_selector: str,
    min_delay: float,
    max_delay: float,
    wait_grow_ms: int = 6000,
) -> int:
    """Крутит страницу, пока количество элементов растёт.
    После каждого скролла явно ждёт, что карточек стало больше (или истечёт wait_grow_ms).
    idle_rounds — сколько подряд «безуспешных ожиданий роста» терпим перед выходом."""
    idle = 0
    last_count = await page.locator(item_selector).count()
    for _ in range(max_scrolls):
        # крутим к концу документа + чуть-чуть, это стабильнее чем mouse.wheel
        await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        # сначала небольшой «вежливый» сон
        await polite_sleep(min_delay, max_delay)
        # теперь ждём, пока появится новая карточка или индикатор подгрузки исчезнет
        try:
            await page.wait_for_function(
                f"(prev) => document.querySelectorAll({item_selector!r}).length > prev",
                arg=last_count,
                timeout=wait_grow_ms,
            )
        except Exception:
            pass
        count = await page.locator(item_selector).count()
        if count == last_count:
            idle += 1
            if idle >= idle_rounds:
                break
        else:
            idle = 0
            last_count = count
    return await page.locator(item_selector).count()
