# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

project = Path(SPECPATH).resolve()
root = project.parent
webview_datas, webview_binaries, webview_imports = collect_all("webview")
pythonnet_datas, pythonnet_binaries, pythonnet_imports = collect_all("pythonnet")
clr_datas, clr_binaries, clr_imports = collect_all("clr_loader")

a = Analysis(
    [str(root / "desktop.py")],
    pathex=[str(root)],
    binaries=webview_binaries + pythonnet_binaries + clr_binaries,
    datas=webview_datas + pythonnet_datas + clr_datas + [
        (str(root / "static"), "static"),
        (str(root / "babeldoc_local.py"), "."),
    ],
    hiddenimports=webview_imports + pythonnet_imports + clr_imports + collect_submodules("webview.platforms"),
    hookspath=[],
    excludes=["tkinter", "matplotlib", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="paper2zh", console=False, icon=str(root / "static" / "icon.ico"))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="paper2zh")
