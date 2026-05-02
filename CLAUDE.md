# CLAUDE.md — инструкция для AI-разработчика

Этот файл — твой первый чек-лист при работе с проектом **«Дзен · Конкуренты»**.
Прочитай целиком до того, как трогать код. Здесь — что есть, как устроено, что
нельзя ломать и какие подводные камни уже найдены.

---

## Что это вообще

Десктопное приложение для глубокого анализа конкурентов в Яндекс.Дзене. Открывается
как нативное окно (через `pywebview`), внутри — UI на FastAPI. Один пользователь,
один локальный процесс, один прогон за раз. Никаких серверов, никакой регистрации.

Под капотом — 6 стадий:

1. **AI расширяет нишу** в 25 поисковых запросов через OpenRouter (Claude Haiku 4.5).
2. **Сбор каналов** через Playwright из двух источников: вкладка «Каналы» поиска +
   карточки статей в обычном поиске.
3. **Похожие каналы** через Дзеновский endpoint `recommend-topic-channels-heads`
   (httpx, параллельно).
4. **Отсев** по минимуму подписчиков (задаёт пользователь).
5. **Детальный анализ** через JSON-API `channel-more` — статьи, просмотры,
   дочитывания, время чтения, точные timestamps. Параллельно через `asyncio.gather`.
6. **AI-классификация** релевантности нише (0..10 + категория + причина).

На выходе — два CSV: каналы (со скором и AI-оценкой) + статьи (с лидами и метриками).

---

## Стек

| Слой | Что |
|---|---|
| Фронт | один `index.html` + `app.js` + `style.css`, без фреймворков. Шрифты TT Severs (RixAI brandbook) |
| Окно | `pywebview` (Cocoa WKWebView на Mac, Edge WebView2 на Win, GTK WebKit на Linux) |
| Веб | FastAPI + uvicorn в фоновом потоке |
| Парсер | httpx + Playwright + sqlite3 |
| AI | OpenRouter API (`anthropic/claude-haiku-4.5` дефолт) |
| Сборка | PyInstaller + GitHub Actions matrix (mac arm64/intel + win + linux) |

---

## Архитектура файлов

```
.
├── dzen_app.py                   # entry point: окно pywebview + сервер в фоне
├── web/
│   ├── server.py                 # FastAPI: /start /stop /status /events /reports
│   ├── index.html                # один HTML
│   └── static/                   # JS/CSS/fonts/иконки
├── dzen_competitors/             # парсер (CLI-режим)
│   ├── dzen_competitors.py       # оркестратор 6 стадий
│   ├── api.py                    # async httpx-клиент к JSON-API Дзена
│   ├── ai.py                     # OpenRouter клиент с budget-лимитом
│   ├── search_parser.py          # Playwright: вкладки «Каналы» и «Статьи»
│   ├── browser.py                # Playwright init + scroll_until_idle
│   ├── parsing_utils.py          # parse_count, channel_slug_from_url, ...
│   ├── reporter.py               # генерация двух CSV
│   ├── storage.py                # SQLite с WAL
│   ├── config.py                 # Config + load_queries (фолбэк YAML)
│   └── queries_example.yaml      # шаблон, если AI недоступен
├── build/
│   ├── dzen.spec                 # PyInstaller spec
│   ├── icon.icns / .ico / .svg   # иконки
│   └── icon_*.png                # PNG разных размеров
├── .github/workflows/build.yml   # матрица сборки на 4 платформы
├── requirements.txt
└── README.md                     # пользовательская документация
```

**Куда пишутся данные при запуске:**

- `data/dzen_competitors.sqlite` — БД всех прогонов
- `data/каналы_*.csv` и `data/статьи_*.csv` — CSV-отчёты
- `logs/run_*.log` — сырой лог фонового процесса (stdout+stderr CLI парсера)

В упакованном `.app` все эти папки лежат рядом с бинарником.

---

## Что точно НЕ ломать

### 1. Headless-режим в продакшене

В `web/server.py::start()` при запуске subprocess **принудительно** ставится
`HEADLESS=true` в env. Не убирать, иначе у пользователя при каждом запросе будет
выскакивать окно Chromium.

```python
env["HEADLESS"] = "true"   # web/server.py — НЕ менять
```

### 2. WAL-режим SQLite

`storage.py::__init__` включает `PRAGMA journal_mode=WAL`. Это нужно, потому что
стадия 5 пишет статьи **параллельно** через `asyncio.gather`. Без WAL — `database
is locked`.

### 3. Канонизация URL статей

`parsing_utils.canonical_article_url` отрезает query-параметры (`rid`, `_csrf`,
`referrer_clid`). Без этого одна статья выглядит как 5 разных URL и ломает дедуп.

### 4. Только `type=article` в `api.py::fetch_channel_feed`

Дзен на вкладке «Статьи» возвращает и `article`, и `brief` (короткие посты).
**Мы намеренно берём только `article`** — для конкурентного анализа статей
брифы не нужны. Если канал в основном пишет брифы, у него будет «0 за 30 дней» —
это корректное поведение, не баг.

### 5. `_csrf` для поиска каналов

API `zen-search?type_filter=publisher` требует `_csrf` cookie, которое генерится
JS на стороне клиента. Поэтому **поиск каналов через httpx без браузера не
работает** — оставляем Playwright. Не пытаться обойти.

### 6. Отдельные httpx.AsyncClient в `ai._call`

Каждый вызов AI открывает свой клиент через `async with httpx.AsyncClient()`.
Не выносить в shared client — Anthropic / OpenRouter могут резать слишком долгие
коннекшены, и refresh нужен.

### 7. `os.killpg(os.getpgid(...))` в `web/server.py::stop`

Используется для корректного SIGINT subprocess'а. На Windows fallback на
`terminate()`. Не упрощать до простого `proc.terminate()` — иначе Playwright
оставляет зомби-процессы Chromium.

---

## Что можно крутить смело

- **Размеры пресетов:** `Config.max_channels_per_query`, `api_concurrency`,
  `api_max_pages_per_channel`. Балансируют скорость vs качество.
- **AI бюджет:** `Config.ai_budget_usd` (дефолт $1). Жёсткий лимит на прогон.
- **Промпты:** `ai.py::expand_niche` и `classify_channels`. При смене промптов
  тестировать на нише «недвижимость» — там много кейсов.
- **Скоринг каналов:** `reporter.py::write_channels_csv`, формула `score`. Веса
  можно подкручивать без боязни сломать.

---

## Как запустить локально

```bash
# Один раз
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Каждый раз
python dzen_app.py
# → откроется окно «Дзен · Конкуренты»
```

Для CLI-режима (без UI):
```bash
DZEN_OPENROUTER_KEY=sk-or-v1-... python dzen_competitors/dzen_competitors.py run \
    --niche "недвижимость" \
    --description "Рынок недвижимости в России для инвесторов" \
    --min-subs 1000
```

---

## Как собрать дистрибутив

Локально под текущую ОС:

```bash
pip install pyinstaller
python -m PyInstaller build/dzen.spec --clean --noconfirm
# → dist/DzenKonkurenty.app (mac) или dist/DzenKonkurenty/ (win/linux)
```

Под все 4 платформы — push тега `v*` в репо:

```bash
git tag v1.0.0
git push origin v1.0.0
```

GitHub Actions сама соберёт и положит файлы в Releases.

---

## Известные ограничения

1. **Дзен ограничивает выдачу.** «Все каналы по нише» получить нельзя — Дзен
   показывает 25-30 каналов за раз. Поэтому стратегия — много разных запросов
   через AI + три источника.

2. **`sort_type=popular` API возвращает 404.** Поэтому в `api.fetch_channel_feed`
   только `regular`. Если когда-то заработает — добавить.

3. **Просмотры/дочитывания у статей <100-200 не отдаются** — Дзен скрывает
   метрики маленьких постов. Это **не наш баг**.

4. **Капча.** При интенсивных прогонах с одного IP Дзен начинает капчить.
   Детектится через `_detect_captcha` в `search_parser.py`. Решение — паузы
   через `MIN_DELAY/MAX_DELAY` в env или прокси через `PROXY_LIST` (ещё не
   реализовано в коде, но архитектурно — одна строка `browser.new_context(proxy=...)`).

5. **Параллельный запуск.** Сейчас один процесс на машину. Несколько
   параллельных прогонов на одном компе **не предусмотрены** — ломаются
   через одну БД и Playwright shared state.

---

## Эстетика интерфейса

Бренд RixAI:

- **Цвета:** глубокий тёмный фон `hsl(220 20% 3%)`, cyan `#1AD8C8` для всех
  акцентов, жёлтый `#FCEC00` **только** для главной CTA-кнопки. Никаких
  фиолетовых градиентов.
- **Шрифты:** `TT Severs DemiBold` для заголовков, `Inter` для body,
  `JetBrains Mono` для логов и чисел.
- **Casing:** заголовки и eyebrow — UPPERCASE с letter-spacing 0.18em.
  Body — sentence case. Кавычки — ёлочки `«»`.
- **Эффекты:** glassmorphism (`backdrop-filter: blur(12-16px)`), conic-rotating
  border на главной карточке (`@property --angle` + 6s linear), pulse на dot'ах,
  shimmer диагональный на активных стадиях.
- **Иконография:** эмодзи в логе допустимы как «дружелюбные сигналы» (🚀🧠🔎📊),
  но в заголовках UI — нет. Цвета зелёный для прогресса (активная/done стадия).

При добавлении новых блоков **не отступать от палитры** — это делает приложение
узнаваемым.

---

## Чек-лист перед коммитом

1. Все `.py` импортируются: `python -c "import dzen_app; from web import server; print('ok')"`
2. Локально приложение запускается и открывает окно: `python dzen_app.py`
3. Smoke-тест: ввести нишу, AI ключ, запустить прогон на 30 секунд, нажать стоп
   — ничего не должно зависнуть.
4. Если правил `dzen.spec` или GitHub Actions — проверить локальную сборку
   `python -m PyInstaller build/dzen.spec --clean --noconfirm`.

---

## Подводные камни, на которые я уже наступал

- **Pywebview на Mac работает только в главном потоке.** Нельзя запускать
  `webview.start()` в треде. Поэтому FastAPI крутится в треде, webview — в main.
- **Webview-окно открывает CSV «инлайн» как текст.** Поэтому `/download`
  отдаёт `application/octet-stream` + `Content-Disposition: attachment`,
  плюс есть отдельный `/save-to-disk` который вызывает системный
  «Save as» через `webview.create_file_dialog`.
- **OpenRouter иногда возвращает 429 / 502** — добавлен exponential backoff в
  `ai._call`.
- **Дзен через `channel-more` отдаёт `_csrf` в `more.link`** — но статьи без него
  всё равно загружаются. Не перетаскивать `_csrf` руками.
- **Локальный `.env` в `dzen_competitors/`** перебивает env-переменные родителя.
  Поэтому `load_dotenv(..., override=False)` — env родителя приоритетнее.

---

## Если что-то непонятно

Открыть `README.md` (для пользователя) и перечитать этот файл. Если всё равно
непонятно — лезть в код в порядке: `dzen_app.py` → `web/server.py` →
`dzen_competitors/dzen_competitors.py` → отдельные модули. Архитектура линейная
без неожиданностей.

Если что-то правишь — **обнови этот файл** одной строкой в разделе
«подводные камни» или «что нельзя ломать».
