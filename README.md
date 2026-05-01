# Дзен · Конкуренты

Десктопное приложение для глубокого анализа конкурентов в Яндекс.Дзене. Работает локально на компьютере пользователя — без серверов и настройки.

## Что делает

1. Принимает нишу и описание от пользователя.
2. AI (Claude через OpenRouter) расширяет нишу в десятки тематических запросов.
3. Скрипт собирает каналы из 3 источников: вкладка «Каналы» в поиске, карточки статей, рекомендации похожих.
4. Отсеивает по фильтру подписчиков.
5. Через JSON-API Дзена тянет статьи и метрики (просмотры, дочитывания, время чтения, дата).
6. AI оценивает каждый канал на релевантность нише.
7. Выдаёт два CSV: каналы (с метриками и AI-оценкой) + статьи.

## Для пользователей

Скачай готовый дистрибутив для своей системы со страницы **[Releases](https://github.com/morf3uzzz/dzen-konkurenty/releases)**:

| ОС | Файл |
|---|---|
| macOS Apple Silicon (M1/M2/M3) | `DzenKonkurenty-mac-arm64.dmg` |
| macOS Intel | `DzenKonkurenty-mac-intel.dmg` |
| Windows 10/11 64-bit | `DzenKonkurenty-win-x64.zip` |
| Linux | `DzenKonkurenty-linux-x64.tar.gz` |

### Установка

**macOS:** двойной клик по `.dmg` → перетащи в Applications. При первом запуске покажется «приложение от неизвестного разработчика» — открой `System Settings → Privacy & Security → Open Anyway`.

**Windows:** распакуй `.zip` → запусти `DzenKonkurenty.exe`. SmartScreen может предупредить — нажми «Подробнее → Выполнить в любом случае».

**Linux:** распакуй `.tar.gz` → запусти `DzenKonkurenty/DzenKonkurenty`.

### Что нужно ввести

1. **OpenRouter API ключ** — получить на [openrouter.ai/keys](https://openrouter.ai/keys), пополнить баланс на $5–10.
2. **Ниша** — например, «недвижимость».
3. **Описание ниши** — чем подробнее, тем точнее AI отсеет нерелевантные каналы.
4. **Минимум подписчиков** — каналы меньше этой величины не попадут в детальный анализ.

Прогон занимает 30–60 минут. Стоимость AI: ~$0.10–0.20 за запуск.

## Для разработки

```bash
# 1. Установить зависимости
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# 2. Запустить десктоп локально
python dzen_app.py
# Откроется http://127.0.0.1:<port> в браузере. Ctrl+C для выхода.

# 3. Только CLI парсер (без UI)
cd dzen_competitors
DZEN_OPENROUTER_KEY=sk-or-... python dzen_competitors.py run \
    --niche "недвижимость" --description "..." --min-subs 1000
```

## Сборка дистрибутивов

Локальная сборка под текущую ОС:

```bash
pip install pyinstaller
python -m PyInstaller build/dzen.spec --clean --noconfirm
# Результат: dist/DzenKonkurenty.app (macOS) или dist/DzenKonkurenty/ (Win/Linux)
```

Для всех 4 платформ собирается через GitHub Actions: пуш тега `v*` → автосборка → файлы в Releases:

```bash
git tag v1.0.0
git push origin v1.0.0
```

## Структура

```
.
├── dzen_app.py              # entry point десктопа
├── web/                     # FastAPI + UI
│   ├── server.py
│   ├── index.html
│   └── static/              # CSS, JS, шрифты
├── dzen_competitors/        # парсер
│   ├── dzen_competitors.py  # CLI
│   ├── api.py               # httpx-клиент к JSON-API Дзена
│   ├── ai.py                # OpenRouter (Claude)
│   ├── search_parser.py     # Playwright-сборщик каналов
│   ├── reporter.py          # CSV
│   ├── storage.py           # SQLite
│   └── ...
├── build/
│   ├── dzen.spec            # PyInstaller config
│   ├── icon.icns/.ico       # иконки
│   └── icon.svg             # исходник
├── .github/workflows/
│   └── build.yml            # GitHub Actions: macOS arm/intel + Win + Linux
└── data/                    # CSV-отчёты пользователя
```

## Лицензия

MIT — для собственного использования и обучения. Внутри RixAI Academy.
