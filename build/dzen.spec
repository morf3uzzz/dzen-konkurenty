# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec для «Дзен · Конкуренты».

Собирает один исполняемый файл, в котором лежат:
- Python runtime
- весь код приложения (UI, парсер, AI-клиент)
- встроенный Chromium от Playwright (через PLAYWRIGHT_BROWSERS_PATH)

Запуск сборки:
    cd <project root>
    python -m PyInstaller build/dzen.spec --clean --noconfirm
"""
import os
import sys
from pathlib import Path

# Корень проекта (где лежит dzen_app.py)
ROOT = Path(SPECPATH).parent.resolve()

# Платформозависимая иконка
if sys.platform == "darwin":
    ICON_FILE = str(ROOT / "build" / "icon.icns")
elif sys.platform == "win32":
    ICON_FILE = str(ROOT / "build" / "icon.ico")
else:
    ICON_FILE = str(ROOT / "build" / "icon_512.png")

# Какие папки тащить целиком в бандл
DATAS = [
    (str(ROOT / "web"), "web"),
    (str(ROOT / "dzen_competitors"), "dzen_competitors"),
    (str(ROOT / "icon.png"), "."),
]

# Hidden imports — модули, которые PyInstaller не находит автоматически
HIDDEN = [
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on",
    "fastapi", "starlette", "anyio",
    "httpx", "h11", "httpcore",
    "yaml", "dotenv",
    "pystray", "PIL",
    "playwright.async_api",
]


a = Analysis(
    [str(ROOT / "dzen_app.py")],
    pathex=[str(ROOT), str(ROOT / "dzen_competitors"), str(ROOT / "web")],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DzenKonkurenty",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=ICON_FILE,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DzenKonkurenty",
)

# macOS .app bundle
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="DzenKonkurenty.app",
        icon=ICON_FILE,
        bundle_identifier="ru.morf3uzzz.dzenkonkurenty",
        info_plist={
            "CFBundleName": "Дзен · Конкуренты",
            "CFBundleDisplayName": "Дзен · Конкуренты",
            "CFBundleVersion": "1.0.0",
            "CFBundleShortVersionString": "1.0.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
