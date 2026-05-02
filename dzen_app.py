"""Entry point десктопного приложения «Дзен · Конкуренты».

Поднимает локальный FastAPI-сервер в фоне и открывает нативное окно через pywebview
(на Mac — Cocoa WKWebView, на Windows — Edge WebView2, на Linux — GTK WebKit).
Никаких вкладок в системном браузере. Закрытие окна = выход из приложения.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path


def _setup_bundled_playwright() -> None:
    """Подсказываем Playwright, где лежит наш встроенный Chromium и какая
    реально архитектура.

    1. PLAYWRIGHT_BROWSERS_PATH → наш бандл рядом с кодом / launcher'ом.
       Иначе Playwright ищет в ~/Library/Caches/ms-playwright, которого у
       пользователя нет.
    2. PLAYWRIGHT_HOST_PLATFORM_OVERRIDE → когда x86_64-сборка крутится на
       Apple Silicon под Rosetta. Тогда Playwright смотрит на os.cpus()[].model
       и думает что мы arm64, ищет chrome-mac-arm64. А у нас в бандле x64."""
    if not getattr(sys, "frozen", False):
        return
    exec_dir = Path(sys.executable).resolve().parent
    candidates = []
    if sys.platform == "darwin" and exec_dir.name == "MacOS" and exec_dir.parent.name == "Contents":
        candidates.append(exec_dir.parent / "Resources" / "playwright-browsers")
    candidates.append(exec_dir / "playwright-browsers")
    for c in candidates:
        if c.exists():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(c)
            break

    # Детект «x86_64-сборка работает под Rosetta на arm64-маке»
    if sys.platform == "darwin":
        import platform as _platform
        py_arch = _platform.machine()  # под Rosetta вернёт x86_64
        # `sysctl.proc_translated == 1` означает «процесс работает под Rosetta».
        try:
            import subprocess
            translated = subprocess.run(
                ["sysctl", "-n", "sysctl.proc_translated"],
                capture_output=True, text=True, timeout=2,
            ).stdout.strip()
        except Exception:
            translated = ""
        if py_arch == "x86_64" and translated == "1":
            # x86_64 binary под Rosetta на Apple Silicon — Playwright должен качать
            # mac-x64 Chromium, форсируем через override
            os.environ.setdefault("PLAYWRIGHT_HOST_PLATFORM_OVERRIDE", "mac15")


_setup_bundled_playwright()


def _resolve_app_dir() -> Path:
    """Где лежат web/ и dzen_competitors/ внутри сборки.
    PyInstaller для .app-bundle копирует data в Contents/Resources/,
    а не рядом с бинарём в Contents/MacOS/. На Win/Linux — рядом с .exe / launcher'ом."""
    if getattr(sys, "frozen", False):
        exec_dir = Path(sys.executable).resolve().parent
        # macOS .app: Contents/MacOS/<exe> → данные в Contents/Resources/
        if sys.platform == "darwin" and exec_dir.name == "MacOS" and exec_dir.parent.name == "Contents":
            resources = exec_dir.parent / "Resources"
            if (resources / "dzen_competitors").exists():
                return resources
        return exec_dir
    return Path(__file__).resolve().parent


APP_DIR = _resolve_app_dir()


def _frozen_cli_mode() -> bool:
    return getattr(sys, "frozen", False) and "--cli" in sys.argv[1:2]


def _run_cli_parser() -> int:
    # CLI-точка — dzen_competitors/dzen_competitors.py.
    # Используем runpy — он корректно настроит __name__ == "__main__" и контекст
    # для dataclass'ов внутри модуля, в отличие от ручного spec_from_file_location.
    sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--cli"]
    sys.path.insert(0, str(APP_DIR / "dzen_competitors"))
    import runpy
    cli_path = APP_DIR / "dzen_competitors" / "dzen_competitors.py"
    runpy.run_path(str(cli_path), run_name="__main__")
    return 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_server_ready(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main() -> int:
    if _frozen_cli_mode():
        return _run_cli_parser()

    sys.path.insert(0, str(APP_DIR))
    sys.path.insert(0, str(APP_DIR / "web"))
    sys.path.insert(0, str(APP_DIR / "dzen_competitors"))

    from web.server import run_server

    port = _free_port()
    url = f"http://127.0.0.1:{port}/"

    # Сервер в daemon-потоке, чтобы он умер вместе с процессом окна
    threading.Thread(
        target=run_server,
        kwargs={"host": "127.0.0.1", "port": port},
        daemon=True,
    ).start()

    if not _wait_server_ready(port):
        print("[!] Сервер не стартанул за 20 секунд", file=sys.stderr)
        return 1

    # Открываем нативное окно
    import webview

    webview.create_window(
        title="Дзен · Конкуренты",
        url=url,
        width=1280,
        height=860,
        min_size=(960, 700),
        background_color="#0A0B10",
        resizable=True,
        confirm_close=False,
    )
    # Блокирующий вызов — выходит когда пользователь закрывает окно
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
