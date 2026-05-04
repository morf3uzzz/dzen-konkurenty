"""Локальный веб-сервер для десктопного приложения.

Архитектура:
- Один пользователь, один процесс, без сессий и cookies.
- API ключ передаётся в каждом запросе с фронта (он живёт только в RAM).
- Файлы пишутся в data-папку рядом с программой.
- Headless Chromium всегда (чтобы не вылезало 12 окон у пользователя).

Запуск:
    python web/server.py        →  http://localhost:8000

Из dzen_app.py (entry-point десктопа) сервер стартует в отдельном потоке.
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles


def _resolve_paths():
    """Возвращает (CODE_ROOT, USER_ROOT).
    CODE_ROOT — где лежат dzen_competitors/web/ и т.п. (read-only, внутри бандла).
    USER_ROOT — куда писать data/ и logs/. Должно быть в писабельной для
    пользователя директории И на видном месте (CSV — главный продукт).
    В dev оба совпадают — корень проекта."""
    if getattr(sys, "frozen", False):
        exec_dir = Path(sys.executable).resolve().parent
        # macOS .app: код в Contents/Resources/. Data — в ~/Documents/DzenKonkurenty,
        # потому что родитель .app — это /Applications, туда писать нельзя
        # (system protection) и не нужно (мусор в системной папке).
        if sys.platform == "darwin" and exec_dir.name == "MacOS" and exec_dir.parent.name == "Contents":
            code_root = exec_dir.parent / "Resources"
            user_root = Path.home() / "Documents" / "DzenKonkurenty"
            return code_root, user_root
        # Win/Linux: пишем рядом с launcher'ом — там пользователь сам выбрал куда
        # распаковать (zip / tar.gz), значит писать туда безопасно и видимо.
        return exec_dir, exec_dir
    proj = Path(__file__).resolve().parent.parent
    return proj, proj


CODE_ROOT, USER_ROOT = _resolve_paths()
COMPETITORS_DIR = CODE_ROOT / "dzen_competitors"
DATA_DIR = USER_ROOT / "data"
LOGS_DIR = USER_ROOT / "logs"
WEB_DIR = Path(__file__).resolve().parent

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _python_interp() -> str:
    """Какой Python запускать для фоновой задачи."""
    if getattr(sys, "frozen", False):
        # В упакованном приложении вызываем сам себя в режиме CLI
        return sys.executable
    venv = CODE_ROOT / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


# ---------- App + state ----------

app = FastAPI()
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


@dataclass
class State:
    process: Optional[subprocess.Popen] = None
    log_path: Optional[Path] = None
    started_at: Optional[float] = None
    niche: Optional[str] = None
    started_at_floor: Optional[float] = None  # mtime, до которого CSV точно не наши


state = State()
_start_lock = asyncio.Lock()


@app.get("/", response_class=HTMLResponse)
async def index():
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


# ---------- Run lifecycle ----------

@app.post("/start")
async def start(req: Request):
    # Лок защищает от гонки на двойном клике «Запустить».
    async with _start_lock:
        if state.process and state.process.poll() is None:
            raise HTTPException(status_code=400, detail="Уже идёт прогон")

        body = await req.json()
        niche = (body.get("niche") or "").strip()
        description = (body.get("description") or "").strip()
        api_key = (body.get("api_key") or "").strip()
        model = (body.get("model") or "").strip() or "anthropic/claude-haiku-4.5"
        try:
            min_subs = int(body.get("min_subs") or 1000)
        except (TypeError, ValueError):
            min_subs = 1000
        if not niche:
            raise HTTPException(status_code=400, detail="Укажи нишу")

        cmd = [
            _python_interp(), "dzen_competitors.py", "run",
            "--niche", niche,
            "--min-subs", str(min_subs),
            "--output-dir", str(DATA_DIR),
        ]
        if description:
            cmd += ["--description", description]
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--cli", "run",
                   "--niche", niche, "--min-subs", str(min_subs),
                   "--output-dir", str(DATA_DIR)]
            if description:
                cmd += ["--description", description]

        ts = time.strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"[^0-9A-Za-zА-Яа-яёЁ_-]", "_", niche)[:40]
        log_path = LOGS_DIR / f"run_{safe}_{ts}.log"
        log_fh = open(log_path, "wb")

        env = os.environ.copy()
        if api_key:
            env["DZEN_OPENROUTER_KEY"] = api_key
        env["DZEN_OPENROUTER_MODEL"] = model
        # В десктопном приложении Chromium ВСЕГДА headless — никаких мигающих окон.
        env["HEADLESS"] = "true"

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=COMPETITORS_DIR if not getattr(sys, "frozen", False) else CODE_ROOT,
                stdout=log_fh, stderr=subprocess.STDOUT,
                env=env, start_new_session=True,
            )
        except OSError as e:
            log_fh.close()
            raise HTTPException(status_code=500, detail=f"Не удалось запустить процесс: {e}")

        state.process = proc
        state.log_path = log_path
        state.started_at = time.time()
        state.started_at_floor = state.started_at - 5   # 5с буфер на разницу часов
        state.niche = niche

        return {"ok": True, "niche": niche}


@app.post("/stop")
async def stop():
    if not state.process or state.process.poll() is not None:
        return {"ok": True, "message": "Нет активного прогона"}
    proc = state.process
    if sys.platform == "win32":
        # На Windows нет killpg/SIGINT. terminate() = TerminateProcess для дочернего;
        # внуки (Chromium) убиваются через taskkill /T /F.
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=5)
        except Exception:
            try: proc.terminate()
            except Exception: pass
    else:
        # POSIX: SIGINT всей группе, чтобы Playwright/Chromium тоже корректно вышли.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        except (ProcessLookupError, OSError):
            try: proc.terminate()
            except Exception: pass

    for _ in range(10):
        time.sleep(0.5)
        if proc.poll() is not None:
            break
    else:
        # Не успел — добиваем
        if sys.platform == "win32":
            try: proc.kill()
            except Exception: pass
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                try: proc.kill()
                except Exception: pass
    return {"ok": True, "message": "Остановлено"}


@app.get("/status")
async def status():
    running = bool(state.process and state.process.poll() is None)
    return {
        "running": running,
        "niche": state.niche,
        "started_at": state.started_at,
        "elapsed": (time.time() - state.started_at) if state.started_at and running else None,
    }


# ---------- Live events ----------

_PATTERNS = [
    (re.compile(r"=== Прогон \d+: «(.+?)» \(мин\. подписчиков: (\d+)\)"),
     lambda m: ("start", f"🚀 Старт: ниша «{m.group(1)}», мин. подписчиков {m.group(2)}")),
    (re.compile(r"AI: ключ OpenRouter (.+)"),
     lambda m: ("info", f"🔐 AI подключён ({m.group(1)})")),
    (re.compile(r"\[1/5\] AI расширяет нишу «(.+?)» в (\d+) запросов"),
     lambda m: ("stage", f"🧠 Стадия 1/5 · расширяю нишу через AI ({m.group(2)} запросов)")),
    (re.compile(r"\[1/5\] AI отключён"),
     lambda m: ("stage", "🧠 Стадия 1/5 · AI отключён, использую шаблон")),
    (re.compile(r"\[1/5\] Запросов готово: (\d+)"),
     lambda m: ("info", f"   ✓ {m.group(1)} запросов готово")),
    (re.compile(r"\[2/5\] Запрос (\d+)/(\d+): «(.+?)»"),
     lambda m: ("stage", f"🔎 Стадия 2/5 · запрос {m.group(1)}/{m.group(2)} — «{m.group(3)}»")),
    (re.compile(r"\[поиск-каналы\] '(.+?)': (\d+) каналов"),
     lambda m: ("info", f"     • вкладка «Каналы»: +{m.group(2)}")),
    (re.compile(r"\[поиск-статьи\] '(.+?)': (\d+) каналов"),
     lambda m: ("info", f"     • из карточек статей: +{m.group(2)}")),
    (re.compile(r"\[2/5\] Найдено уникальных каналов: (\d+)"),
     lambda m: ("info", f"   ✓ всего уникальных каналов: {m.group(1)}")),
    (re.compile(r"\[3/5\] Прошли фильтр \(>= (\d+) подписчиков\): (\d+) из (\d+)"),
     lambda m: ("stage", f"🪣 Стадия 3/5 · отсев — {m.group(2)} из {m.group(3)} прошли фильтр (≥ {m.group(1)})")),
    (re.compile(r"\[4/5\] Детальный анализ (\d+) каналов через API"),
     lambda m: ("stage", f"📊 Стадия 4/5 · собираю метрики для {m.group(1)} каналов")),
    (re.compile(r"\[4/5\] прогресс: (\d+)/(\d+) каналов"),
     lambda m: ("info", f"   • {m.group(1)}/{m.group(2)} каналов готово")),
    (re.compile(r"\[4/5\] Собрано статей: (\d+)"),
     lambda m: ("info", f"   ✓ собрано статей: {m.group(1)}")),
    (re.compile(r"\[5/5\] AI классифицирует (\d+) каналов"),
     lambda m: ("stage", f"🎯 Стадия 5/5 · AI оценивает релевантность {m.group(1)} каналов")),
    (re.compile(r"AI classify батч (\d+): (\d+)/(\d+), потрачено \$([\d.]+)"),
     lambda m: ("info", f"     батч {m.group(1)}: {m.group(2)}/{m.group(3)} (+${m.group(4)})")),
    (re.compile(r"=== Готово\. Каналов (\d+), статей (\d+), классифицировано (\d+) ==="),
     lambda m: ("done", f"✅ Готово · {m.group(1)} каналов · {m.group(2)} статей · {m.group(3)} классифицировано")),
    (re.compile(r"AI потрачено: \$([\d.]+)"),
     lambda m: ("info", f"💸 Всего AI: ${m.group(1)}")),
    (re.compile(r"Прерывание пользователем"),
     lambda m: ("warn", "🛑 Прервано пользователем")),
    (re.compile(r"AI ошибка: OpenRouter отверг ключ"),
     lambda m: ("warn", "⚠️ AI-ключ невалиден или баланс кончился. Используется шаблон запросов — качество ниже.")),
    (re.compile(r"AI ошибка: (.+)"),
     lambda m: ("warn", f"⚠️ AI: {m.group(1)[:120]}")),
    (re.compile(r"КАПЧА"),
     lambda m: ("warn", "⚠️ Дзен показал капчу — увеличь паузу или попробуй позже")),
]


def _humanize(line: str):
    cleaned = re.sub(r"^\d{2}:\d{2}:\d{2}\s+(INFO|WARNING|ERROR)\s+", "", line).strip()
    for rx, fn in _PATTERNS:
        m = rx.search(cleaned)
        if m:
            return fn(m)
    return None


@app.get("/events")
async def events(offset: int = 0):
    if not state.log_path or not state.log_path.exists():
        return {"offset": 0, "lines": []}
    size = state.log_path.stat().st_size
    with open(state.log_path, "rb") as f:
        f.seek(offset)
        chunk = f.read()
    if not chunk:
        return {"offset": size, "lines": []}
    text = chunk.decode("utf-8", errors="replace")
    out = []
    for raw in text.splitlines():
        h = _humanize(raw)
        if h:
            out.append({"kind": h[0], "text": h[1]})
    return {"offset": size, "lines": out}


# ---------- Reports ----------

REPORT_RE = re.compile(r"^(статьи|каналы)_(.+)_(\d{8}_\d{6})\.csv$")


@app.get("/last-report")
async def last_report():
    """Возвращает CSV-пару именно последнего прогона (после state.started_at_floor)."""
    if state.started_at_floor is None:
        return {"report": None}
    floor = state.started_at_floor
    groups: dict[str, dict] = {}
    for p in DATA_DIR.glob("*.csv"):
        if p.stat().st_mtime < floor:
            continue
        m = REPORT_RE.match(p.name)
        if not m:
            continue
        kind, niche, ts = m.groups()
        key = f"{niche}__{ts}"
        groups.setdefault(key, {
            "niche": niche, "timestamp": ts,
            "articles": None, "channels": None,
            "stats": None,
        })
        groups[key][
            "articles" if kind == "статьи" else "channels"
        ] = {"name": p.name, "size": p.stat().st_size}
    if not groups:
        return {"report": None}
    # Если несколько прогонов попало — возьмём самый поздний по timestamp
    latest = sorted(groups.values(), key=lambda g: g["timestamp"], reverse=True)[0]
    if latest.get("channels"):
        articles_path = DATA_DIR / latest["articles"]["name"] if latest.get("articles") else None
        latest["stats"] = _quick_stats(DATA_DIR / latest["channels"]["name"], articles_path)
    return {"report": latest}


@app.get("/reports")
async def reports():
    groups: dict[str, dict] = {}
    for p in sorted(DATA_DIR.glob("*.csv"), reverse=True):
        m = REPORT_RE.match(p.name)
        if not m:
            continue
        kind, niche, ts = m.groups()
        key = f"{niche}__{ts}"
        groups.setdefault(key, {
            "niche": niche, "timestamp": ts,
            "articles": None, "channels": None,
            "stats": None,
        })
        groups[key][
            "articles" if kind == "статьи" else "channels"
        ] = {"name": p.name, "size": p.stat().st_size}

    # Подтягиваем краткую статистику по каждому прогону из CSV каналов
    for g in groups.values():
        if not g["channels"]:
            continue
        articles_path = DATA_DIR / g["articles"]["name"] if g.get("articles") else None
        g["stats"] = _quick_stats(DATA_DIR / g["channels"]["name"], articles_path)
    return {"reports": list(groups.values())}


def _quick_stats(channels_csv: Path, articles_csv: Optional[Path] = None) -> Optional[dict]:
    """Считает быструю статистику по CSV каналов: всего, средний размер, топ-канал.
    Если передан articles_csv — добавляет точное число статей (по строкам)."""
    try:
        import csv
        with open(channels_csv, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return None
        total = len(rows)
        subs = [int(r.get("Подписчики") or 0) for r in rows if (r.get("Подписчики") or "").strip().isdigit()]
        avg = int(sum(subs) / len(subs)) if subs else 0
        top = rows[0].get("Название канала") or rows[0].get("Канал (slug)") or ""
        # «Профильные» = прямые конкуренты. Учитываем и старое имя категории
        # «профильный» (CSV до v1.0.2), и новое «прямой конкурент» (текущее).
        DIRECT = {"профильный", "прямой конкурент"}
        prof = sum(1 for r in rows if (r.get("Категория") or "").strip().lower() in DIRECT)
        # Считаем статьи по реальным строкам, а не по размеру файла.
        articles_total = None
        if articles_csv and articles_csv.exists():
            try:
                with open(articles_csv, encoding="utf-8-sig") as af:
                    articles_total = sum(1 for _ in csv.DictReader(af))
            except Exception:
                articles_total = None
        return {
            "total_channels": total,
            "avg_subscribers": avg,
            "top_channel": top[:60],
            "profile_count": prof,
            "articles_total": articles_total,
        }
    except Exception:
        return None


@app.get("/download/{name}")
async def download(name: str):
    if "/" in name or ".." in name:
        raise HTTPException(status_code=400)
    p = DATA_DIR / name
    if not p.exists():
        raise HTTPException(status_code=404)
    # application/octet-stream + явный attachment — гарантирует, что браузер/webview
    # не откроет файл «инлайн», а покажет диалог «Сохранить как».
    from urllib.parse import quote
    return FileResponse(
        p,
        filename=name,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
        },
    )


@app.post("/save-to-disk")
async def save_to_disk(req: Request):
    """Десктопный режим: показывает системный 'Save as' и копирует файл туда.
    Используется кнопкой 'Сохранить' в UI. Если pywebview недоступен, отдаёт ошибку."""
    body = await req.json()
    name = (body.get("name") or "").strip()
    if "/" in name or ".." in name:
        raise HTTPException(status_code=400)
    src = DATA_DIR / name
    if not src.exists():
        raise HTTPException(status_code=404)
    try:
        import webview
        windows = webview.windows
        if not windows:
            raise RuntimeError("no webview window")
        win = windows[0]
        # SAVE_DIALOG возвращает путь, куда пользователь хочет сохранить.
        target = win.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=name,
            file_types=("CSV (*.csv)", "All files (*.*)"),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"webview недоступен: {e}")
    if not target:
        return {"ok": False, "cancelled": True}
    target_path = target if isinstance(target, str) else target[0]
    import shutil
    shutil.copy2(src, target_path)
    return {"ok": True, "path": target_path}


@app.get("/live-preview")
async def live_preview():
    """Во время прогона возвращает топ найденных каналов из последнего run.
    Читает SQLite БД парсера в режиме read-only — не мешает write-сессии."""
    db_path = COMPETITORS_DIR / "data" / "dzen_competitors.sqlite"
    if not db_path.exists():
        return {"channels": [], "total": 0}
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        # Последний прогон
        run = c.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if not run:
            return {"channels": [], "total": 0}
        run_id = run["id"]
        # Все каналы этого прогона
        rows = c.execute("""
            SELECT DISTINCT ch.slug, ch.title, ch.subscribers, ch.relevance, ch.category
            FROM channels ch
            JOIN channel_hits h ON h.slug = ch.slug AND h.run_id = ?
            ORDER BY COALESCE(ch.relevance, -1) DESC, COALESCE(ch.subscribers, 0) DESC
            LIMIT 12
        """, (run_id,)).fetchall()
        total = c.execute(
            "SELECT COUNT(DISTINCT slug) AS n FROM channel_hits WHERE run_id=?",
            (run_id,),
        ).fetchone()["n"]
        conn.close()
        out = []
        for r in rows:
            out.append({
                "slug": r["slug"],
                "title": (r["title"] or r["slug"])[:60],
                "subscribers": r["subscribers"],
                "relevance": r["relevance"],
                "category": r["category"],
            })
        return {"channels": out, "total": total}
    except sqlite3.OperationalError:
        return {"channels": [], "total": 0}


@app.get("/top-channels")
async def top_channels(timestamp: str, limit: int = 30):
    """Возвращает топ-N каналов прогона с расширенными полями для UI-таблицы."""
    if not re.match(r"^\d{8}_\d{6}$", timestamp):
        raise HTTPException(status_code=400, detail="bad timestamp")
    csv_path = next(DATA_DIR.glob(f"каналы_*_{timestamp}.csv"), None)
    if not csv_path:
        raise HTTPException(status_code=404, detail="canal CSV not found")
    import csv
    rows: list[dict] = []
    try:
        with open(csv_path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append({
                    "rank": _safe_int(r.get("Ранг")),
                    "relevance": _safe_int(r.get("Релевантность 0-10")),
                    "category": r.get("Категория") or "",
                    "title": r.get("Название канала") or r.get("Канал (slug)") or "",
                    "url": r.get("Ссылка на канал") or "",
                    "subscribers": _safe_int(r.get("Подписчики")),
                    "score": _safe_float(r.get("Скор")),
                    "articles_30d": _safe_int(r.get("Статей за 30 дней")),
                    "median_views": _safe_int(r.get("Медиана просмотров")),
                    "max_views": _safe_int(r.get("Макс просмотров")),
                    "read_through": r.get("Дочитываемость, %") or "",
                    "top1": r.get("Топ-1 заголовок") or "",
                    "top1_url": r.get("Топ-1 ссылка") or "",
                    "top1_views": _safe_int(r.get("Топ-1 просмотры")),
                })
                if len(rows) >= limit:
                    break
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"rows": rows}


def _safe_int(v):
    try: return int(v) if v not in (None, "") else None
    except (ValueError, TypeError): return None


def _safe_float(v):
    try: return float(v) if v not in (None, "") else None
    except (ValueError, TypeError): return None


@app.post("/delete-report")
async def delete_report(req: Request):
    """Удаляет пару CSV (статьи + каналы) одного прогона по timestamp."""
    body = await req.json()
    ts = (body.get("timestamp") or "").strip()
    if not re.match(r"^\d{8}_\d{6}$", ts):
        raise HTTPException(status_code=400, detail="bad timestamp")
    deleted = 0
    for p in DATA_DIR.glob(f"*_{ts}.csv"):
        try:
            p.unlink()
            deleted += 1
        except OSError:
            pass
    return {"ok": True, "deleted": deleted}


@app.get("/open-data-folder")
async def open_data_folder():
    """Открывает папку с CSV в Finder/Explorer."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(DATA_DIR)])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", str(DATA_DIR)])
        else:
            subprocess.Popen(["xdg-open", str(DATA_DIR)])
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------- OpenRouter models ----------

@app.post("/models")
async def list_models(req: Request):
    body = await req.json()
    api_key = (body.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="Нужен ключ")
    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if r.status_code == 401:
            raise HTTPException(status_code=401, detail="Невалидный ключ OpenRouter")
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"OpenRouter HTTP {r.status_code}")
        data = r.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Ошибка сети: {e}")

    out = []
    for m in data.get("data") or []:
        mid = m.get("id") or ""
        if not mid.startswith("anthropic/claude"):
            continue
        pricing = m.get("pricing") or {}
        try:
            in_per_m = float(pricing.get("prompt", 0)) * 1_000_000
            out_per_m = float(pricing.get("completion", 0)) * 1_000_000
        except (TypeError, ValueError):
            in_per_m = out_per_m = 0
        out.append({
            "id": mid, "name": m.get("name") or mid,
            "context_length": m.get("context_length"),
            "in_per_m": round(in_per_m, 3),
            "out_per_m": round(out_per_m, 3),
        })
    out.sort(key=lambda x: x["in_per_m"])
    return {"models": out}


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Запускает сервер. Используется и в dev-режиме, и из dzen_app.py."""
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run_server()
