# PyInstaller 설정: 콘솔 창 없는 단일 exe. 실행:  pyinstaller apphelper.spec
# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["run_apphelper.py"],
    pathex=[],
    datas=[("pages/index.html", "pages")],
    hiddenimports=[],
    excludes=["tkinter", "unittest", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Apphelper",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
)
