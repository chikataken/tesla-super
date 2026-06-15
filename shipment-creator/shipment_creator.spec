# PyInstaller build spec for the Shipment Creator GUI.
#
#   .venv\Scripts\pyinstaller shipment_creator.spec --noconfirm
#
# Produces dist\ShipmentCreator\ShipmentCreator.exe (onedir). The exe starts the
# local FastAPI server and opens the GUI in the default browser. /api/run re-invokes
# this same exe with a --pipeline marker (see app.py) to run the pipeline.
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [('static', 'static')]          # the single-file frontend, served at /
binaries = []
hiddenimports = []

# Packages whose data files / dynamic submodules PyInstaller's static analysis
# misses. Playwright in particular ships a node "driver" we must carry along so the
# CDP-attach BOL/scan steps work in the frozen build.
for pkg in ('uvicorn', 'playwright', 'keyring', 'pdfplumber', 'pdfminer',
            'win32ctypes', 'pystray', 'PIL'):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:
        pass

hiddenimports += collect_submodules('uvicorn')
hiddenimports += [
    'keyring.backends.Windows',         # Windows Credential Manager backend
    'win32ctypes.pywin32',
    'win32timezone',
    'pystray._win32',                   # tray backend on Windows
]

# Our own modules — most are imported lazily inside endpoint functions, which static
# analysis catches, but list them explicitly so a refactor can't silently drop one.
hiddenimports += [
    'app', 'main', 'config', 'settings_store', 'paths', 'chrome_cdp', 'auth',
    'tesla_bol', 'sd_api', 'sd_scrape', 'consolidation', 'excel_ingest',
    'grouping', 'models', 'pdf_read', 'transit',
]

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'pytest', 'matplotlib', 'PIL.ImageQt'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ShipmentCreator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,           # windowed: no terminal. The system-tray icon (Open/Quit) controls the app.
    disable_windowed_traceback=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ShipmentCreator',
)
