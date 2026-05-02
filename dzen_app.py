"""Entry point десктопного приложения «Дзен · Конкуренты».

Поднимает локальный FastAPI-сервер в фоне и открывает нативное окно через pywebview
(на Mac — Cocoa WKWebView, на Windows — Edge WebView2, на Linux — GTK WebKit).
Никаких вкладок в системном браузере. Закрытие окна = выход из приложения.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path


def _resolve_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = _resolve_app_dir()


def _frozen_cli_mode() -> bool:
    return getattr(sys, "frozen", False) and "--cli" in sys.argv[1:2]


def _run_cli_parser() -> int:
    sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--cli"]
    sys.path.insert(0, str(APP_DIR / "dzen_competitors"))
    import importlib
    cli_module = importlib.import_module("dzen_competitors")
    cli_module.main()
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
