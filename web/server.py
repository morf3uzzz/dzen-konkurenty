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


def _resolve_root() -> Path:
    """В упакованном приложении (PyInstaller) данные лежат рядом с .app/.exe.
    В режиме разработки — в корне проекта."""
    if getattr(sys, "frozen", False):
        # PyInstaller bundle
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = _resolve_root()
COMPETITORS_DIR = ROOT / "dzen_competitors"
DATA_DIR = ROOT / "data"
LOGS_DIR = ROOT / "logs"
WEB_DIR = Path(__file__).resolve().parent

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _python_interp() -> str:
    """Какой Python запускать для фоновой задачи."""
    if getattr(sys, "frozen", False):
        # В упакованном приложении вызываем сам себя в режиме CLI
        return sys.executable
    venv = ROOT / ".venv" / "bin" / "python"
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


state = State()


@app.get("/", response_class=HTMLResponse)
async def index():
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


# ---------- Run lifecycle ----------

@app.post("/start")
async def start(req: Request):
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
    # Если запускаемся из упакованного приложения — в env скажем модулю dzen_competitors
    # что он работает как подкоманда того же frozen-бинарника.
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
    # В десктопном приложении Chromium всегда headless.
    env.setdefault("HEADLESS", "true")

    proc = subprocess.Popen(
        cmd,
        cwd=COMPETITORS_DIR if not getattr(sys, "frozen", False) else ROOT,
        stdout=log_fh, stderr=subprocess.STDOUT,
        env=env, start_new_session=True,
    )

    state.process = proc
    state.log_path = log_path
    state.started_at = time.time()
    state.niche = niche

    return {"ok": True, "niche": niche}


@app.post("/stop")
async def stop():
    if not state.process or state.process.poll() is not None:
        return {"ok": True, "message": "Нет активного прогона"}
    try:
        os.killpg(os.getpgid(state.process.pid), signal.SIGINT)
    except (ProcessLookupError, AttributeError):
        try:
            state.process.terminate()
        except Exception:
            pass
    for _ in range(10):
        time.sleep(0.5)
        if state.process.poll() is not None:
            break
    else:
        try:
            os.killpg(os.getpgid(state.process.pid), signal.SIGTERM)
        except (ProcessLookupError, AttributeError):
            try:
                state.process.kill()
            except Exception:
                pass
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
    (re.compile(r"\[1/6\] AI расширяет нишу «(.+?)» в (\d+) запросов"),
     lambda m: ("stage", f"🧠 Стадия 1/6 · расширяю нишу через AI ({m.group(2)} запросов)")),
    (re.compile(r"\[1/6\] AI отключён"),
     lambda m: ("stage", "🧠 Стадия 1/6 · AI отключён, использую шаблон")),
    (re.compile(r"\[1/6\] Запросов готово: (\d+)"),
     lambda m: ("info", f"   ✓ {m.group(1)} запросов готово")),
    (re.compile(r"\[2/6\] Запрос (\d+)/(\d+): «(.+?)»"),
     lambda m: ("stage", f"🔎 Стадия 2/6 · запрос {m.group(1)}/{m.group(2)} — «{m.group(3)}»")),
    (re.compile(r"\[поиск-каналы\] '(.+?)': (\d+) каналов"),
     lambda m: ("info", f"     • вкладка «Каналы»: +{m.group(2)}")),
    (re.compile(r"\[поиск-статьи\] '(.+?)': (\d+) каналов"),
     lambda m: ("info", f"     • из карточек статей: +{m.group(2)}")),
    (re.compile(r"\[2/6\] Найдено уникальных каналов: (\d+)"),
     lambda m: ("info", f"   ✓ всего уникальных каналов: {m.group(1)}")),
    (re.compile(r"\[3/6\] \+(\d+) новых каналов из похожих, всего: (\d+)"),
     lambda m: ("stage", f"🌐 Стадия 3/6 · похожие каналы +{m.group(1)} (всего {m.group(2)})")),
    (re.compile(r"\[4/6\] Прошли фильтр \(>= (\d+) подписчиков\): (\d+) из (\d+)"),
     lambda m: ("stage", f"🪣 Стадия 4/6 · отсев — {m.group(2)} из {m.group(3)} прошли фильтр (≥ {m.group(1)})")),
    (re.compile(r"\[5/6\] Детальный анализ (\d+) каналов через API"),
     lambda m: ("stage", f"📊 Стадия 5/6 · собираю метрики для {m.group(1)} каналов")),
    (re.compile(r"\[5/6\] Собрано статей: (\d+)"),
     lambda m: ("info", f"   ✓ собрано статей: {m.group(1)}")),
    (re.compile(r"\[6/6\] AI классифицирует (\d+) каналов"),
     lambda m: ("stage", f"🎯 Стадия 6/6 · AI оценивает релевантность {m.group(1)} каналов")),
    (re.compile(r"AI classify батч (\d+): (\d+)/(\d+), потрачено \$([\d.]+)"),
     lambda m: ("info", f"     батч {m.group(1)}: {m.group(2)}/{m.group(3)} (+${m.group(4)})")),
    (re.compile(r"=== Готово\. Каналов (\d+), статей (\d+), классифицировано (\d+) ==="),
     lambda m: ("done", f"✅ Готово · {m.group(1)} каналов · {m.group(2)} статей · {m.group(3)} классифицировано")),
    (re.compile(r"AI потрачено: \$([\d.]+)"),
     lambda m: ("info", f"💸 Всего AI: ${m.group(1)}")),
    (re.compile(r"Прерывание пользователем"),
     lambda m: ("warn", "🛑 Прервано пользователем")),
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
        })
        groups[key][
            "articles" if kind == "статьи" else "channels"
        ] = {"name": p.name, "size": p.stat().st_size}
    return {"reports": list(groups.values())}


@app.get("/download/{name}")
async def download(name: str):
    if "/" in name or ".." in name:
        raise HTTPException(status_code=400)
    p = DATA_DIR / name
    if not p.exists():
        raise HTTPException(status_code=404)
    return FileResponse(p, filename=name, media_type="text/csv")


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
