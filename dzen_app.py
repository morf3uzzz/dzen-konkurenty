"""Entry point десктопного приложения «Дзен · Конкуренты».

Запускает локальный FastAPI-сервер, открывает браузер на главную страницу,
держится до Ctrl+C или закрытия окна терминала. На сборке через PyInstaller
получим .app/.exe/AppImage с встроенным Chromium и Python.
"""
from __future__ import annotations

import os
import signal
import socket
import sys
import threading
import time
import webbrowser
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

    # Сервер в фоне, browser в основном
    t = threading.Thread(
        target=run_server,
        kwargs={"host": "127.0.0.1", "port": port},
        daemon=True,
    )
    t.start()

    if not _wait_server_ready(port):
        print("[!] Сервер не стартанул за 20 секунд", file=sys.stderr)
        return 1

    # Дружелюбный баннер в консоль (виден только если запустить из терминала)
    banner = f"""
╔═══════════════════════════════════════════════╗
║  Дзен · Конкуренты                            ║
║                                               ║
║  Открыто: {url:<35} ║
║                                               ║
║  Закрой это окно или нажми Ctrl+C, чтобы      ║
║  выйти. CSV-отчёты — в папке data/.           ║
╚═══════════════════════════════════════════════╝
"""
    print(banner)
    webbrowser.open(url)

    # Ждём сигнала о выходе
    stop_event = threading.Event()

    def _on_signal(*_):
        print("\nЗавершаю работу…")
        stop_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
