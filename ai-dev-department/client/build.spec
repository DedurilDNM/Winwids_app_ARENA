# PyInstaller spec for AI Dev Department client (Windows .exe)
# Build: pyinstaller build.spec  (run on Windows with PySide6 installed)

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

a = Analysis(
    ["app/main.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=collect_submodules("PySide6") + [
        "app.api_client",
        "app.auth_store",
        "app.windows.login",
        "app.windows.main",
        "app.windows.project_detail",
        "app.windows.accounts_tab",
        "app.windows.vnc_view",
        "app.widgets.api_worker",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="AI Dev Department Client",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
